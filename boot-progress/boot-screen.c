/* SPDX-License-Identifier: MIT
 * Early boot UI. Uses fbdev only; never changes display timings or storage.
 * Text uses a small original bitmap alphabet; no external fonts/assets.
 */
#define _POSIX_C_SOURCE 200809L
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/kd.h>
#include <linux/fs.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define W 1280
#define H 720
#define LOG_CAP (128 * 1024)
static uint32_t frame[H][W];
static volatile sig_atomic_t running = 1;
static void done(int n) { (void)n; running = 0; }
struct glyph { char c; unsigned char row[7]; };
static const struct glyph glyphs[] = {
 {'A',{14,17,17,31,17,17,17}}, {'B',{30,17,17,30,17,17,30}},
 {'C',{14,17,16,16,16,17,14}}, {'D',{30,17,17,17,17,17,30}},
 {'E',{31,16,16,30,16,16,31}}, {'F',{31,16,16,30,16,16,16}},
 {'G',{14,17,16,23,17,17,15}}, {'H',{17,17,17,31,17,17,17}},
 {'I',{14,4,4,4,4,4,14}}, {'J',{7,2,2,2,18,18,12}},
 {'K',{17,18,20,24,20,18,17}}, {'L',{16,16,16,16,16,16,31}},
 {'M',{17,27,21,21,17,17,17}}, {'N',{17,25,21,19,17,17,17}},
 {'O',{14,17,17,17,17,17,14}}, {'P',{30,17,17,30,16,16,16}},
 {'Q',{14,17,17,17,21,18,13}}, {'R',{30,17,17,30,20,18,17}},
 {'S',{15,16,16,14,1,1,30}}, {'T',{31,4,4,4,4,4,4}},
 {'U',{17,17,17,17,17,17,14}}, {'V',{17,17,17,17,17,10,4}},
 {'W',{17,17,17,21,21,21,10}}, {'X',{17,17,10,4,10,17,17}},
 {'Y',{17,17,10,4,4,4,4}}, {'Z',{31,1,2,4,8,16,31}},
 {'0',{14,17,19,21,25,17,14}}, {'1',{4,12,4,4,4,4,14}},
 {'2',{14,17,1,2,4,8,31}}, {'3',{30,1,1,14,1,1,30}},
 {'4',{2,6,10,18,31,2,2}}, {'5',{31,16,16,30,1,1,30}},
 {'6',{14,16,16,30,17,17,14}}, {'7',{31,1,2,4,8,8,8}},
 {'8',{14,17,17,14,17,17,14}}, {'9',{14,17,17,15,1,1,14}},
 {'.',{0,0,0,0,0,12,12}}, {':',{0,12,12,0,12,12,0}},
 {'/',{1,2,2,4,8,8,16}}, {'-',{0,0,0,31,0,0,0}},
 {'_',{0,0,0,0,0,0,31}}, {'%',{17,2,4,8,17,0,0}},
 {'(',{2,4,8,8,8,4,2}}, {')',{8,4,2,2,2,4,8}},
 {'[',{14,8,8,8,8,8,14}}, {']',{14,2,2,2,2,2,14}},
 {'=',{0,0,31,0,31,0,0}}, {'+',{0,4,4,31,4,4,0}},
 {'!',{4,4,4,4,4,0,4}}, {'?',{14,17,1,2,4,0,4}},
 {'>',{16,8,4,2,4,8,16}}, {'<',{1,2,4,8,4,2,1}},
 {'|',{4,4,4,4,4,4,4}}, {'*',{0,21,14,31,14,21,0}},
 {'\'',{4,4,8,0,0,0,0}}, {'"',{10,10,0,0,0,0,0}},
 {',',{0,0,0,0,0,4,8}}, {'\\',{16,8,8,4,2,2,1}},
 {' ',{0,0,0,0,0,0,0}}
};
static void rect(int x,int y,int w,int h,uint32_t c) {
 for(int j=y;j<y+h;j++) for(int i=x;i<x+w;i++)
  if(i>=0 && i<W && j>=0 && j<H) frame[j][i]=c;
}
static void text(int x,int y,int s,const char *p,uint32_t c,int limit) {
 for(int k=0;*p && k<limit;p++,k++,x+=6*s) {
  unsigned char ch=(unsigned char)toupper((unsigned char)*p);
  const unsigned char *rows=NULL;
  for(size_t i=0;i<sizeof(glyphs)/sizeof(*glyphs);i++)
   if(glyphs[i].c==ch) { rows=glyphs[i].row; break; }
  if(!rows) {for(size_t i=0;i<sizeof(glyphs)/sizeof(*glyphs);i++)if(glyphs[i].c=='?')rows=glyphs[i].row;}
  for(int j=0;j<7;j++) for(int i=0;i<5;i++)
   if(rows[j] & (1<<(4-i))) rect(x+i*s,y+j*s,s,s,c);
 }
}
static const char *stages[]={"STARTING STEGRAOS","CHECKING SD CARD",
 "EXPANDING LINUX FILESYSTEM","MOUNTING LINUX FILESYSTEM",
 "CONFIGURING HARDWARE","STARTING SYSTEM SERVICES"};
static void render(int stage,const char *detail,int percent,long elapsed,
                   char lines[6][96],int tick) {
 uint32_t bg=0x0c1424,fg=0xf1f5fa,muted=0x9cacc4;
 uint32_t accent=stage<0?0xff8787:0x36d5e4;
 rect(0,0,W,H,bg); rect(60,55,6,80,accent);
 text(86,56,6,"STEGRAOS",fg,30);
 text(88,115,2,"FIRST BOOT AND STARTUP",muted,60);
 text(64,207,4,stage<0?"SETUP STOPPED":stages[stage<=5?stage:0],fg,46);
 text(64,258,2,detail,muted,94);
 rect(64,306,1152,20,0x24344b);
 if(stage<0) rect(64,306,1152,20,accent);
 else if(percent>=0) rect(64,306,1152*percent/100,20,accent);
 else { int x=(tick*38)%1352-200; int a=x<0?0:x,b=x+200>1152?1152:x+200;
        if(b>a) rect(64+a,306,b-a,20,accent); }
 char status[100];
 if(percent>=0 && stage>=0) snprintf(status,sizeof(status),"CURRENT RESIZE PASS: %d%%    ELAPSED %ld:%02ld",percent,elapsed/60,elapsed%60);
 else snprintf(status,sizeof(status),"%s    ELAPSED %ld:%02ld",stage<0?"SEE THE LOG BELOW":"WORKING - NO TIME ESTIMATE",elapsed/60,elapsed%60);
 text(64,352,2,status,accent,94);
 rect(60,410,1160,220,0x070d18); rect(60,410,1160,2,0x293c54);
 text(78,429,2,"LIVE CONSOLE",muted,90);
 for(int i=0;i<6;i++) text(78,463+i*25,2,lines[i],fg,94);
 text(64,664,2,stage<0?"POWER OFF BEFORE REMOVING THE SD. CHECK STEGRA-LOGS ON THE SD.":
      "PLEASE KEEP POWER CONNECTED. DO NOT REMOVE THE SD WHILE SETUP IS WORKING.",muted,96);
}
/* Normalize the bounded raw log like a terminal (CR and backspace included).
 * Percent is recognized only when a command actually prints a numeric percent.
 * Otherwise the moving bar explicitly has no percentage/time estimate.
 */
static int logtail(const char *path,char lines[6][96]) {
 FILE *f=fopen(path,"rb"); char row[2048]={0};size_t n=0;int c,pct=-1;
 memset(lines,0,6*96);
 if(!f) return -1;
 while((c=fgetc(f))!=EOF) {
  if(c=='\b') { if(n) row[--n]=0; }
  else if(c=='\r') { n=0; row[0]=0; }
  else if(c=='\n') { memmove(lines,lines+1,5*96);snprintf(lines[5],96,"%.95s",row);n=0;row[0]=0; }
  else if(c>=32 && c<127 && n<sizeof(row)-1) {
   row[n++]=(char)c;row[n]=0;
   if(c=='%') {size_t a=n-1;while(a && isdigit((unsigned char)row[a-1]))a--;
    if(a<n-1 && (n-1-a)<=3) {int v=atoi(row+a);if(v<=100)pct=v;}}
  }
 }
 if(n) {memmove(lines,lines+1,5*96);snprintf(lines[5],96,"%.95s",row);}
 fclose(f);return pct;
}
static int record(int argc,char **argv) {
 int fd[2]={-1,-1};size_t used[2]={0,0};char buf[4096];ssize_t n;
 for(int i=0;i<2 && i+2<argc;i++) {
  fd[i]=open(argv[i+2],O_WRONLY|O_APPEND|O_CREAT|O_CLOEXEC,0600);
  struct stat st;if(fd[i]>=0 && fstat(fd[i],&st)==0)used[i]=(size_t)st.st_size;
 }
 while((n=read(STDIN_FILENO,buf,sizeof(buf)))!=0) {
  if(n<0) {if(errno==EINTR)continue;break;}
  /* Logging errors never close the pipe early or terminate resize2fs. */
  for(int i=0;i<2;i++) if(fd[i]>=0 && used[i]<LOG_CAP) {
   size_t m=(size_t)n;if(m>LOG_CAP-used[i])m=LOG_CAP-used[i];size_t off=0;
   while(off<m) {ssize_t z=write(fd[i],buf+off,m-off);if(z<0 && errno==EINTR)continue;if(z<=0)break;off+=(size_t)z;}
   used[i]+=m;
  }
  size_t off=0;while(off<(size_t)n) {ssize_t z=write(STDOUT_FILENO,buf+off,(size_t)n-off);if(z<0 && errno==EINTR)continue;if(z<=0)break;off+=(size_t)z;}
 }
 for(int i=0;i<2;i++)if(fd[i]>=0)close(fd[i]);
 return n<0?1:0;
}
static uint32_t le32(const unsigned char *p) {
 return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;
}
/* Read-only size gate: do not invoke an offline resizer again after a prior
 * mount when the filesystem already occupies the partition. Never shrink. */
static int probe_size(const char *path) {
 int fd=open(path,O_RDONLY|O_CLOEXEC);if(fd<0){perror("open root read-only");return 2;}
 unsigned char sb[1024];struct stat st;uint64_t bytes=0;
 if(fstat(fd,&st) || pread(fd,sb,sizeof(sb),1024)!=(ssize_t)sizeof(sb)) {
  fprintf(stderr,"Cannot read root superblock\n");close(fd);return 2;
 }
 if(S_ISBLK(st.st_mode)) {if(ioctl(fd,BLKGETSIZE64,&bytes)){perror("BLKGETSIZE64");close(fd);return 2;}}
 else if(S_ISREG(st.st_mode) && st.st_size>0)bytes=(uint64_t)st.st_size;
 else {fprintf(stderr,"Root is neither a block device nor a test file\n");close(fd);return 2;}
 close(fd);
 unsigned log=le32(sb+24);
 if(sb[56]!=0x53 || sb[57]!=0xef || log>6){fprintf(stderr,"Invalid ext4 superblock\n");return 2;}
 uint64_t blocks=le32(sb+4);if(le32(sb+96)&0x80)blocks|=(uint64_t)le32(sb+336)<<32;
 uint64_t blocksize=1024ULL<<log,available=bytes/blocksize;
 if(!blocks || blocks>available){fprintf(stderr,"Root filesystem exceeds its partition; refusing shrink\n");return 2;}
 printf("ROOT SIZE: %llu BLOCKS; PARTITION: %llu BLOCKS (%llu BYTES EACH)\n",
        (unsigned long long)blocks,(unsigned long long)available,(unsigned long long)blocksize);
 puts(blocks==available?"ALREADY EXPANDED - SKIPPING RESIZE":"RESIZE REQUIRED");
 return blocks==available?3:0;
}
struct fb {int fd,tty,oldmode;struct fb_var_screeninfo v;struct fb_fix_screeninfo f;unsigned char *mem;};
static int openfb(struct fb *b) {
 memset(b,0,sizeof(*b));b->fd=b->tty=-1;b->oldmode=-1;
 b->fd=open("/dev/fb0",O_RDWR|O_CLOEXEC);if(b->fd<0)return -1;
 if(ioctl(b->fd,FBIOGET_VSCREENINFO,&b->v)||ioctl(b->fd,FBIOGET_FSCREENINFO,&b->f))goto fail;
 unsigned bytes=b->v.bits_per_pixel/8;
 if(b->f.type!=FB_TYPE_PACKED_PIXELS || b->f.visual!=FB_VISUAL_TRUECOLOR ||
    (bytes!=2 && bytes!=3 && bytes!=4) || b->v.bits_per_pixel%8 ||
    b->v.xres<320 || b->v.yres<320 || b->v.xres>4096 || b->v.yres>4096 ||
    b->v.xoffset>b->v.xres_virtual || b->v.yoffset>b->v.yres_virtual ||
    b->v.xres>b->v.xres_virtual-b->v.xoffset || b->v.yres>b->v.yres_virtual-b->v.yoffset ||
    (uint64_t)(b->v.xoffset+b->v.xres)*bytes>b->f.line_length ||
    (uint64_t)(b->v.yoffset+b->v.yres)*b->f.line_length>b->f.smem_len ||
    b->f.smem_len>128*1024*1024 || b->f.smem_len==0)goto fail;
 struct fb_bitfield *ch[]={&b->v.red,&b->v.green,&b->v.blue,&b->v.transp};
 uint32_t mask=0;
 for(int i=0;i<4;i++) {if(ch[i]->length>8 || ch[i]->offset>b->v.bits_per_pixel || ch[i]->length>b->v.bits_per_pixel-ch[i]->offset || ch[i]->msb_right)goto fail;
  if(!ch[i]->length)continue;
  uint32_t m=((1U<<ch[i]->length)-1)<<ch[i]->offset;if(mask&m)goto fail;mask|=m;}
 if(!b->v.red.length || !b->v.green.length || !b->v.blue.length)goto fail;
 b->mem=mmap(NULL,b->f.smem_len,PROT_READ|PROT_WRITE,MAP_SHARED,b->fd,0);
 if(b->mem==MAP_FAILED)goto fail;
 b->tty=open("/dev/tty1",O_RDWR|O_CLOEXEC);
 if(b->tty>=0 && ioctl(b->tty,KDGETMODE,&b->oldmode)==0)ioctl(b->tty,KDSETMODE,KD_GRAPHICS);
 fprintf(stderr,"FRAMEBUFFER %ux%u bpp=%u stride=%u rotation_correction=180 old_tty_mode=%d\n",
         b->v.xres,b->v.yres,b->v.bits_per_pixel,b->f.line_length,b->oldmode);
 ioctl(b->fd,FBIOBLANK,FB_BLANK_UNBLANK);return 0;
fail:close(b->fd);b->fd=-1;return -1;
}
static uint32_t channel(unsigned n,struct fb_bitfield f) {
 if(!f.length)return 0;
 return ((n*((1U<<f.length)-1)+127)/255)<<f.offset;
}
static void present(struct fb *b) {
 int portrait=b->v.yres>b->v.xres;unsigned bytes=b->v.bits_per_pixel/8;
 unsigned lw=portrait?b->v.yres:b->v.xres,lh=portrait?b->v.xres:b->v.yres;
 for(unsigned py=0;py<b->v.yres;py++)for(unsigned px=0;px<b->v.xres;px++) {
  unsigned lx=portrait?py:px,ly=portrait?b->v.xres-1-px:py;
  /* The real OLED test established that the scanout is inverted by 180.
   * Rotate only physical scanout; exported logical previews stay upright. */
  uint32_t c=frame[H-1-ly*H/lh][W-1-lx*W/lw];
  uint32_t v=channel((c>>16)&255,b->v.red)|channel((c>>8)&255,b->v.green)|channel(c&255,b->v.blue)|channel(255,b->v.transp);
  size_t off=(py+b->v.yoffset)*(size_t)b->f.line_length+(px+b->v.xoffset)*bytes;
  memcpy(b->mem+off,&v,bytes);
 }
}
int main(int argc,char **argv) {
 signal(SIGPIPE,SIG_IGN);
 if(argc==3 && !strcmp(argv[1],"--probe-size"))return probe_size(argv[2]);
 if(argc>=3 && !strcmp(argv[1],"--record"))return record(argc,argv);
 char lines[6][96]={{0}};
 if(argc>=6 && !strcmp(argv[1],"--preview")) {
  snprintf(lines[0],96,"SD CARD FOUND. ROOT LABEL: STGRA-B");
  snprintf(lines[1],96,"RESIZE2FS 1.45.6 (20-MAR-2020)");
  snprintf(lines[2],96,"RESIZING THE FILESYSTEM TO THE LINUX PARTITION");
  snprintf(lines[3],96,"OUTPUT IS SAVED TO STEGRA-LOGS ON THE SD CARD");
  render(atoi(argv[3]),argv[4],argc>6?atoi(argv[6]):-1,atol(argv[5]),lines,10);
  FILE *f=fopen(argv[2],"wb");if(!f)return 1;fprintf(f,"P6\n%d %d\n255\n",W,H);
  for(int y=0;y<H;y++)for(int x=0;x<W;x++){uint32_t c=frame[y][x];unsigned char p[]={c>>16,c>>8,c};fwrite(p,1,3,f);}
  return fclose(f)?1:0;
 }
 if(argc!=3 || strcmp(argv[1],"--watch"))return 2;
 signal(SIGTERM,done);signal(SIGINT,done);
 struct timespec begin;clock_gettime(CLOCK_MONOTONIC,&begin);
 struct fb b;int active=0,tick=0;
 while(running) {
  char path[512],detail[256]="PREPARING EARLY BOOT",s[32];int stage=0;
  snprintf(path,sizeof(path),"%s/state",argv[2]);FILE *f=fopen(path,"r");
  if(f){if(fgets(s,sizeof(s),f))stage=atoi(s);if(fgets(detail,sizeof(detail),f))detail[strcspn(detail,"\r\n")]=0;fclose(f);}
  snprintf(path,sizeof(path),"%s/boot.log",argv[2]);int pct=logtail(path,lines);
  if(stage!=2)pct=-1;
  struct timespec now;clock_gettime(CLOCK_MONOTONIC,&now);
  render(stage,detail,pct,now.tv_sec-begin.tv_sec,lines,tick++);
  if(!active && tick<120)active=openfb(&b)==0;
  if(active)present(&b);
  struct timespec delay={0,500000000};nanosleep(&delay,NULL);
 }
 if(active){if(b.tty>=0){int rc=ioctl(b.tty,KDSETMODE,KD_TEXT);fprintf(stderr,"TEXT_CONSOLE_HANDOFF rc=%d\n",rc);close(b.tty);}munmap(b.mem,b.f.smem_len);close(b.fd);}
 return 0;
}
