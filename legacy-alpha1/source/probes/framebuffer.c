/* Switchdeck framebuffer diagnostic. No modeset, panel power or GPU access. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/kd.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

struct surface {
    struct fb_var_screeninfo v;
    struct fb_fix_screeninfo f;
    uint8_t *pixels;
    size_t length;
    unsigned bytes, width, height, rotate;
};

static volatile sig_atomic_t interrupted;
static void on_signal(int number) { interrupted = number; }

static int layout(struct surface *s)
{
    const struct fb_var_screeninfo *v = &s->v;
    if (s->f.type != FB_TYPE_PACKED_PIXELS || s->f.visual != FB_VISUAL_TRUECOLOR ||
        v->grayscale || v->nonstd ||
        (v->bits_per_pixel != 16 && v->bits_per_pixel != 24 && v->bits_per_pixel != 32) ||
        !v->xres || !v->yres || v->xres > 4096 || v->yres > 4096 ||
        (s->rotate != 0 && s->rotate != 90)) return -1;
    s->bytes = v->bits_per_pixel / 8;
    uint64_t right = (uint64_t)v->xoffset + v->xres;
    uint64_t bottom = (uint64_t)v->yoffset + v->yres;
    if (right > v->xres_virtual || bottom > v->yres_virtual ||
        right * s->bytes > s->f.line_length) return -1;
    uint64_t end = (bottom - 1) * s->f.line_length + right * s->bytes;
    if (!s->f.smem_len || end > s->f.smem_len || end > 128 * 1024 * 1024) return -1;
    struct fb_bitfield fields[] = {v->red, v->green, v->blue, v->transp};
    uint32_t used = 0;
    for (unsigned i = 0; i < 4; ++i) {
        struct fb_bitfield b = fields[i];
        if (b.msb_right || b.length > 8 || (!b.length && i < 3) ||
            b.offset > v->bits_per_pixel || b.length > v->bits_per_pixel - b.offset) return -1;
        if (!b.length) continue;
        uint32_t mask = ((1u << b.length) - 1) << b.offset;
        if (used & mask) return -1;
        used |= mask;
    }
    s->length = (size_t)end;
    s->width = s->rotate ? v->yres : v->xres;
    s->height = s->rotate ? v->xres : v->yres;
    return 0;
}

static uint32_t channel(unsigned value, struct fb_bitfield field)
{
    return field.length ? ((value * ((1u << field.length) - 1) + 127) / 255) << field.offset : 0;
}

static size_t offset(const struct surface *s, unsigned x, unsigned y)
{
    unsigned px = s->rotate ? y : x;
    unsigned py = s->rotate ? s->v.yres - 1 - x : y;
    return ((size_t)py + s->v.yoffset) * s->f.line_length +
           ((size_t)px + s->v.xoffset) * s->bytes;
}

static void pixel(struct surface *s, unsigned x, unsigned y, uint32_t rgb)
{
    if (x >= s->width || y >= s->height) return;
    uint32_t value = channel((rgb >> 16) & 255, s->v.red) |
                     channel((rgb >> 8) & 255, s->v.green) |
                     channel(rgb & 255, s->v.blue) | channel(255, s->v.transp);
    size_t at = offset(s, x, y);
    for (unsigned i = 0; i < s->bytes; ++i) s->pixels[at + i] = (uint8_t)(value >> (8 * i));
}

static void box(struct surface *s, unsigned x, unsigned y, unsigned w, unsigned h, uint32_t rgb)
{
    for (unsigned dy = 0; dy < h; ++dy)
        for (unsigned dx = 0; dx < w; ++dx) pixel(s, x + dx, y + dy, rgb);
}

/* Original 5x7 bitmap alphabet for this diagnostic. */
static const uint8_t letters[26][7] = {
    {14,17,17,31,17,17,17}, {30,17,17,30,17,17,30}, {14,17,16,16,16,17,14},
    {30,17,17,17,17,17,30}, {31,16,16,30,16,16,31}, {31,16,16,30,16,16,16},
    {14,17,16,23,17,17,14}, {17,17,17,31,17,17,17}, {31,4,4,4,4,4,31},
    {7,2,2,2,18,18,12}, {17,18,20,24,20,18,17}, {16,16,16,16,16,16,31},
    {17,27,21,21,17,17,17}, {17,25,25,21,19,19,17}, {14,17,17,17,17,17,14},
    {30,17,17,30,16,16,16}, {14,17,17,17,21,18,13}, {30,17,17,30,20,18,17},
    {15,16,16,14,1,1,30}, {31,4,4,4,4,4,4}, {17,17,17,17,17,17,14},
    {17,17,17,17,17,10,4}, {17,17,17,21,21,27,17}, {17,17,10,4,10,17,17},
    {17,17,10,4,4,4,4}, {31,1,2,4,8,16,31}
};
static const uint8_t digits[10][7] = {
    {14,17,19,21,25,17,14}, {4,12,4,4,4,4,14}, {14,17,1,2,4,8,31},
    {30,1,1,14,1,1,30}, {2,6,10,18,31,2,2}, {31,16,16,30,1,1,30},
    {14,16,16,30,17,17,14}, {31,1,2,4,8,8,8}, {14,17,17,14,17,17,14},
    {14,17,17,15,1,1,14}
};

static void label(struct surface *s, unsigned x, unsigned y, unsigned scale, const char *text)
{
    for (; *text; ++text, x += 6 * scale) {
        const uint8_t *rows = NULL;
        if (*text >= 'A' && *text <= 'Z') rows = letters[*text - 'A'];
        if (*text >= '0' && *text <= '9') rows = digits[*text - '0'];
        for (unsigned dy = 0; dy < 7; ++dy) {
            unsigned bits = rows ? rows[dy] : (*text == '-' && dy == 3 ? 31 : 0);
            for (unsigned dx = 0; dx < 5; ++dx)
                if (bits & (16u >> dx)) box(s, x + dx * scale, y + dy * scale, scale, scale, 0xffffff);
        }
    }
}

static void pattern(struct surface *s)
{
    box(s, 0, 0, s->width, s->height, 0x0b1626);
    box(s, 0, 0, s->width, 4, 0xffffff);
    box(s, 0, s->height - 4, s->width, 4, 0xffffff);
    box(s, 0, 0, 4, s->height, 0xffffff);
    box(s, s->width - 4, 0, 4, s->height, 0xffffff);
    label(s, 62, 55, 6, "SWITCHDECK BOOT0 R6");
    label(s, 62, 117, 3, "DISPLAY TEST - TAKE A PHOTO");
    const uint32_t colors[] = {0xff0000, 0x00ff00, 0x0000ff, 0xffffff};
    const char *names[] = {"RED", "GREEN", "BLUE", "WHITE"};
    for (unsigned i = 0; i < 4; ++i) {
        box(s, 60 + i * 290, 180, 280, 260, colors[i]);
        label(s, 70 + i * 290, 460, 4, names[i]);
    }
    for (unsigned x = 0; x < 1160; ++x) {
        unsigned g = x * 255 / 1159;
        box(s, 60 + x, 520, 1, 60, g * 0x010101);
    }
    label(s, 62, 615, 3, "CPU FRAMEBUFFER - STEAM UI NOT STARTED");
    label(s, 62, 652, 2, "20 SECOND TEST - THEN RETURN TO HEKATE");
    label(s, 14, 14, 3, "1");
    label(s, 1245, 14, 3, "2");
    label(s, 14, 686, 3, "3");
    label(s, 1245, 686, 3, "4");
}

static struct surface sample(unsigned w, unsigned h, unsigned bits, unsigned rotate)
{
    struct surface s = {0};
    s.v.xres = w; s.v.yres = h;
    s.v.xres_virtual = w + 3; s.v.yres_virtual = h + 2;
    s.v.xoffset = 1; s.v.yoffset = 1;
    s.v.bits_per_pixel = bits;
    s.v.red = (struct fb_bitfield){bits == 16 ? 11 : 16, bits == 16 ? 5 : 8, 0};
    s.v.green = (struct fb_bitfield){bits == 16 ? 5 : 8, bits == 16 ? 6 : 8, 0};
    s.v.blue = (struct fb_bitfield){0, bits == 16 ? 5 : 8, 0};
    if (bits == 32) s.v.transp = (struct fb_bitfield){24, 8, 0};
    s.f.type = FB_TYPE_PACKED_PIXELS; s.f.visual = FB_VISUAL_TRUECOLOR;
    s.f.line_length = (w + 3) * (bits / 8) + 16;
    s.f.smem_len = s.f.line_length * (h + 2);
    s.rotate = rotate;
    return s;
}

static int self_test(void)
{
    for (unsigned bits = 16; bits <= 32; bits += 8) {
        for (unsigned rotate = 0; rotate <= 90; rotate += 90) {
            struct surface s = sample(7, 11, bits, rotate);
            if (layout(&s)) return 10;
            uint8_t *data = malloc(s.f.smem_len + 64);
            if (!data) return 11;
            memset(data, 0xa5, s.f.smem_len + 64);
            s.pixels = data + 32;
            box(&s, 0, 0, s.width, s.height, 0xff0000);
            pixel(&s, s.width, 0, 0xffffff); /* Must clip. */
            pixel(&s, 0, s.height, 0xffffff);
            for (unsigned at = 0; at < s.f.smem_len + 64; ++at) {
                int p = (int)at - 32;
                int y = p < 0 ? -1 : p / (int)s.f.line_length;
                int xbyte = p < 0 ? -1 : p % (int)s.f.line_length;
                int inside = y >= 1 && y < 12 && xbyte >= (int)s.bytes && xbyte < 8 * (int)s.bytes;
                uint8_t expected = 0xa5;
                if (inside) {
                    unsigned component = (unsigned)xbyte % s.bytes;
                    expected = bits == 16 ? (component == 1 ? 0xf8 : 0) : (component >= 2 ? 0xff : 0);
                }
                if (data[at] != expected) { free(data); return 12; }
            }
            pixel(&s, 0, 0, 0x0000ff);
            size_t corner = (rotate ? 11u : 1u) * s.f.line_length + s.bytes;
            if (s.pixels[corner] != (bits == 16 ? 31 : 255)) { free(data); return 13; }
            free(data);
            struct surface bad = s;
            bad.f.smem_len = (uint32_t)s.length - 1;
            if (!layout(&bad)) return 14;
            bad = s; bad.f.line_length = 1;
            if (!layout(&bad)) return 15;
            bad = s; bad.v.green = bad.v.red;
            if (!layout(&bad)) return 16;
            bad = s; bad.v.xoffset = UINT32_MAX;
            if (!layout(&bad)) return 17;
        }
    }
    puts("FB_SELF_TEST_OK formats=16,24,32 rotation=0,90 bounds=checked invalid_layouts=rejected");
    return 0;
}

static int preview(const char *path)
{
    struct surface s = sample(720, 1280, 32, 90);
    if (layout(&s)) return 1;
    s.pixels = calloc(1, s.length);
    if (!s.pixels) return 1;
    pattern(&s);
    FILE *file = fopen(path, "wb");
    if (!file) { free(s.pixels); return 1; }
    fprintf(file, "P6\n%u %u\n255\n", s.width, s.height);
    for (unsigned y = 0; y < s.height; ++y)
        for (unsigned x = 0; x < s.width; ++x) {
            size_t at = offset(&s, x, y);
            uint8_t rgb[] = {s.pixels[at + 2], s.pixels[at + 1], s.pixels[at]};
            if (fwrite(rgb, 1, 3, file) != 3) { fclose(file); free(s.pixels); return 1; }
        }
    free(s.pixels);
    return fclose(file) ? 1 : 0;
}

static int hardware(void)
{
    int result = 1, tty = -1, old_mode = -1, graphics = 0;
    uint8_t *mapped = MAP_FAILED, *saved = NULL;
    struct surface s = {0};
    int fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
    if (fd < 0) { perror("FB_OPEN_FAILED /dev/fb0"); return 2; }
    if (ioctl(fd, FBIOGET_FSCREENINFO, &s.f) || ioctl(fd, FBIOGET_VSCREENINFO, &s.v)) {
        perror("FB_QUERY_FAILED"); goto done;
    }
    printf("FB_INFO id=%.16s native=%ux%u virtual=%ux%u offset=%u,%u bpp=%u stride=%u bytes=%u rotate_hint=%u\n",
           s.f.id, s.v.xres, s.v.yres, s.v.xres_virtual, s.v.yres_virtual,
           s.v.xoffset, s.v.yoffset, s.v.bits_per_pixel, s.f.line_length, s.f.smem_len, s.v.rotate);
    printf("FB_CHANNELS red=%u:%u green=%u:%u blue=%u:%u alpha=%u:%u\n",
           s.v.red.offset, s.v.red.length, s.v.green.offset, s.v.green.length,
           s.v.blue.offset, s.v.blue.length, s.v.transp.offset, s.v.transp.length);
    if (s.v.xres == 720 && s.v.yres == 1280) s.rotate = 90;
    else if (s.v.xres != 1280 || s.v.yres != 720) {
        puts("FB_UNSUPPORTED_RESOLUTION no_mode_change_attempted"); goto done;
    }
    if (layout(&s)) { puts("FB_UNSUPPORTED_LAYOUT no_write_attempted"); goto done; }
    mapped = mmap(NULL, s.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (mapped == MAP_FAILED) { perror("FB_MMAP_FAILED"); goto done; }
    saved = malloc(s.length);
    s.pixels = malloc(s.length);
    if (!saved || !s.pixels) { puts("FB_ALLOCATION_FAILED"); goto done; }
    memcpy(saved, mapped, s.length);
    memcpy(s.pixels, saved, s.length);
    pattern(&s);
    tty = open("/dev/tty0", O_RDWR | O_CLOEXEC);
    if (tty >= 0 && !ioctl(tty, KDGETMODE, &old_mode) && old_mode == KD_TEXT) {
        if (!ioctl(tty, KDSETMODE, KD_GRAPHICS)) graphics = 1;
        else perror("FB_CONSOLE_GRAPHICS_FAILED");
    }
    printf("FB_CONSOLE graphics_guard=%d\n", graphics);
    struct sigaction action = {0};
    action.sa_handler = on_signal;
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    memcpy(mapped, s.pixels, s.length);
    if (ioctl(fd, FBIOBLANK, FB_BLANK_UNBLANK)) perror("FB_UNBLANK_FAILED");
    else puts("FB_UNBLANK_OK");
    struct fb_var_screeninfo pan = s.v;
    pan.activate = FB_ACTIVATE_NOW | FB_ACTIVATE_FORCE;
    if (ioctl(fd, FBIOPAN_DISPLAY, &pan)) perror("FB_PRESENT_FAILED");
    else {
        printf("FB_DRAW_OK logical=%ux%u software_rotation=%u hold_seconds=20 visibility=unconfirmed\n",
               s.width, s.height, s.rotate);
        puts("FB_TEST_PATTERN red-green-blue-white gradient border corner-labels; CPU drawing only");
        result = 0;
    }
    for (unsigned second = 0; second < 20 && !interrupted; ++second) {
        struct timespec delay = {1, 0};
        while (nanosleep(&delay, &delay) && errno == EINTR && !interrupted) {}
    }
    memcpy(mapped, saved, s.length);
    pan = s.v;
    pan.activate = FB_ACTIVATE_NOW | FB_ACTIVATE_FORCE;
    if (ioctl(fd, FBIOPAN_DISPLAY, &pan)) { perror("FB_RESTORE_PRESENT_FAILED"); result = 1; }
    if (interrupted) result = 128 + interrupted;
    puts("FB_PIXELS_RESTORED visibility_requires_user_photo");
done:
    if (graphics && ioctl(tty, KDSETMODE, old_mode)) { perror("FB_CONSOLE_RESTORE_FAILED"); result = 1; }
    if (tty >= 0) close(tty);
    free(saved);
    free(s.pixels);
    if (mapped != MAP_FAILED) munmap(mapped, s.length);
    close(fd);
    return result;
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc == 2 && !strcmp(argv[1], "--self-test")) return self_test();
    if (argc == 3 && !strcmp(argv[1], "--preview")) return preview(argv[2]);
    if (argc != 1) { fputs("Usage: framebuffer-test [--self-test | --preview FILE.ppm]\n", stderr); return 64; }
    return hardware();
}
