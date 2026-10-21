// SPDX-License-Identifier: GPL-2.0-or-later
// Physical USB keyboards/mice -> existing Gamescope EIS socket.
// No EVIOCGRAB, uinput, XTest, compositor replacement or raw key logging.
#define _GNU_SOURCE
#include <libei.h>
#include <libinput.h>
#include <libudev.h>
#include <linux/input-event-codes.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <poll.h>
#include <signal.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define NPHYS 32
#define NEI 8
struct physical { struct libinput_device *dev; bool removing; char node[128]; unsigned char key[KEY_CNT],button[KEY_CNT]; };
struct virtual { struct ei_device *dev; bool resumed; };
static struct physical phys[NPHYS];
static struct virtual virt[NEI];
static unsigned int keys[KEY_CNT],buttons[KEY_CNT];
static struct udev *udev;
static struct libinput *li;
static struct ei *ei;
static volatile sig_atomic_t stopping;
static bool disconnected,got_pong,input_failed;
static uint32_t sequence;
static uint64_t motions,key_events,button_events,scrolls;
static double mouse_y_scale=1.0;

static uint64_t now_us(void) { struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (uint64_t)t.tv_sec*1000000+t.tv_nsec/1000; }
static void stop(int sig) { (void)sig;stopping=1; }
static bool property(struct udev_device *d,const char *n,const char *v) {
    const char *p=udev_device_get_property_value(d,n);return p && !strcmp(p,v);
}
static bool accepted(struct udev_device *d) {
    const char *n=udev_device_get_devnode(d);
    return n && !strncmp(n,"/dev/input/event",16) && property(d,"ID_BUS","usb") &&
        !property(d,"ID_INPUT_JOYSTICK","1") && !property(d,"ID_INPUT_TOUCHSCREEN","1") &&
        !property(d,"ID_INPUT_TABLET","1") &&
        (property(d,"ID_INPUT_KEYBOARD","1") || property(d,"ID_INPUT_MOUSE","1"));
}
static int restricted_open(const char *path,int flags,void *unused) {
    (void)unused;const char *leaf=strrchr(path,'/');
    struct udev_device *d=udev_device_new_from_subsystem_sysname(udev,"input",leaf?leaf+1:path);
    bool ok=d && accepted(d);if(d)udev_device_unref(d);
    if(!ok)return -EACCES;
    int fd=open(path,flags|O_CLOEXEC);return fd<0?-errno:fd;
}
static void restricted_close(int fd,void *unused) { (void)unused;close(fd); }
static const struct libinput_interface input_interface={.open_restricted=restricted_open,.close_restricted=restricted_close};
static struct ei_device *receiver(enum ei_device_capability cap) {
    for(int i=0;i<NEI;i++)if(virt[i].dev && virt[i].resumed && ei_device_has_capability(virt[i].dev,cap))return virt[i].dev;
    return NULL;
}
static bool ready(void) {
    return receiver(EI_DEVICE_CAP_KEYBOARD) && receiver(EI_DEVICE_CAP_POINTER) && receiver(EI_DEVICE_CAP_BUTTON) && receiver(EI_DEVICE_CAP_SCROLL);
}
static void frame(struct ei_device *d) { ei_device_frame(d,ei_now(ei)); }
static void send_key(unsigned int key,bool press) {
    struct ei_device *d=receiver(EI_DEVICE_CAP_KEYBOARD);if(!d)return;
    ei_device_keyboard_key(d,key,press);frame(d);key_events++;
}
static void send_button(unsigned int button,bool press) {
    struct ei_device *d=receiver(EI_DEVICE_CAP_BUTTON);if(!d)return;
    ei_device_button_button(d,button,press);frame(d);button_events++;
}
static void release_physical(struct physical *p) {
    for(unsigned int k=0;k<KEY_CNT;k++) {
        if(p->key[k]) {p->key[k]=0;if(keys[k] && --keys[k]==0)send_key(k,false);}
        if(p->button[k]) {p->button[k]=0;if(buttons[k] && --buttons[k]==0)send_button(k,false);}
    }
}
static void forget_state(void) {
    memset(keys,0,sizeof keys);memset(buttons,0,sizeof buttons);
    for(int i=0;i<NPHYS;i++){memset(phys[i].key,0,sizeof phys[i].key);memset(phys[i].button,0,sizeof phys[i].button);}
}
static void dispatch_ei(void) {
    ei_dispatch(ei);
    struct ei_event *e;
    while((e=ei_get_event(ei))) {
        enum ei_event_type type=ei_event_get_type(e);
        struct ei_device *d=ei_event_get_device(e);
        switch(type) {
        case EI_EVENT_CONNECT:fprintf(stderr,"USB_EI_CONNECTED\n");break;
        case EI_EVENT_SEAT_ADDED:
            ei_seat_bind_capabilities(ei_event_get_seat(e),EI_DEVICE_CAP_POINTER,EI_DEVICE_CAP_POINTER_ABSOLUTE,
                EI_DEVICE_CAP_BUTTON,EI_DEVICE_CAP_SCROLL,EI_DEVICE_CAP_KEYBOARD,NULL);break;
        case EI_EVENT_DEVICE_ADDED:
            for(int i=0;i<NEI;i++)if(!virt[i].dev){virt[i].dev=ei_device_ref(d);fprintf(stderr,"USB_EI_DEVICE name=%s\n",ei_device_get_name(d));break;}
            break;
        case EI_EVENT_DEVICE_RESUMED:
            for(int i=0;i<NEI;i++)if(virt[i].dev==d && !virt[i].resumed){virt[i].resumed=true;ei_device_start_emulating(d,++sequence);fprintf(stderr,"USB_EI_RESUMED\n");}
            break;
        case EI_EVENT_DEVICE_PAUSED:
        case EI_EVENT_DEVICE_REMOVED:
            for(int i=0;i<NEI;i++)if(virt[i].dev==d){virt[i].resumed=false;if(type==EI_EVENT_DEVICE_REMOVED){ei_device_unref(d);virt[i].dev=NULL;}}
            /* Paused devices may not receive events. Do not replay stale holds. */
            forget_state();fprintf(stderr,"USB_EI_DEVICE_UNAVAILABLE type=%d\n",type);break;
        case EI_EVENT_DISCONNECT:disconnected=true;forget_state();fprintf(stderr,"USB_EI_DISCONNECTED\n");break;
        case EI_EVENT_PONG:got_pong=true;break;
        default:break;
        }
        ei_event_unref(e);
    }
}
static void add_device(struct udev_device *d) {
    if(!accepted(d))return;
    const char *node=udev_device_get_devnode(d);int slot=-1;
    for(int i=0;i<NPHYS;i++){if(phys[i].dev && !strcmp(phys[i].node,node))return;if(!phys[i].dev && slot<0)slot=i;}
    if(slot<0){fprintf(stderr,"USB_DEVICE_LIMIT\n");return;}
    struct libinput_device *dev=libinput_path_add_device(li,node);
    if(!dev){fprintf(stderr,"USB_ADD_FAILED node=%s\n",node);return;}
    /* path_add_device returns a borrowed reference. Keep our own until the
     * DEVICE_REMOVED event is handled; the event itself owns another ref. */
    struct physical *p=&phys[slot];p->dev=libinput_device_ref(dev);snprintf(p->node,sizeof p->node,"%s",node);libinput_device_set_user_data(dev,p);
    fprintf(stderr,"USB_DEVICE_ADDED node=%s name=%s keyboard=%d pointer=%d\n",node,libinput_device_get_name(dev),
        libinput_device_has_capability(dev,LIBINPUT_DEVICE_CAP_KEYBOARD),libinput_device_has_capability(dev,LIBINPUT_DEVICE_CAP_POINTER));
}
static void dispatch_input(void) {
    if(libinput_dispatch(li)<0){fprintf(stderr,"USB_INPUT_DISPATCH_FAILED\n");input_failed=true;stopping=1;return;}
    struct libinput_event *e;
    while((e=libinput_get_event(li))) {
        enum libinput_event_type t=libinput_event_get_type(e);
        struct physical *p=libinput_device_get_user_data(libinput_event_get_device(e));
        if(p && t==LIBINPUT_EVENT_DEVICE_REMOVED) {
            release_physical(p);fprintf(stderr,"USB_DEVICE_REMOVED node=%s\n",p->node);
            libinput_device_unref(p->dev);memset(p,0,sizeof *p);
        } else if(p && !p->removing && ready()) {
            if(t==LIBINPUT_EVENT_KEYBOARD_KEY) {
                struct libinput_event_keyboard *k=libinput_event_get_keyboard_event(e);
                unsigned int code=libinput_event_keyboard_get_key(k);bool pressed=libinput_event_keyboard_get_key_state(k)==LIBINPUT_KEY_STATE_PRESSED;
                if(code<KEY_CNT && p->key[code]!=pressed){p->key[code]=pressed;if(pressed){if(keys[code]++==0)send_key(code,true);}else if(keys[code] && --keys[code]==0)send_key(code,false);}
            } else if(t==LIBINPUT_EVENT_POINTER_BUTTON) {
                struct libinput_event_pointer *b=libinput_event_get_pointer_event(e);
                unsigned int code=libinput_event_pointer_get_button(b);bool pressed=libinput_event_pointer_get_button_state(b)==LIBINPUT_BUTTON_STATE_PRESSED;
                if(code<KEY_CNT && p->button[code]!=pressed){p->button[code]=pressed;if(pressed){if(buttons[code]++==0)send_button(code,true);}else if(buttons[code] && --buttons[code]==0)send_button(code,false);}
            } else if(t==LIBINPUT_EVENT_POINTER_MOTION) {
                struct libinput_event_pointer *m=libinput_event_get_pointer_event(e);struct ei_device *d=receiver(EI_DEVICE_CAP_POINTER);
                ei_device_pointer_motion(d,libinput_event_pointer_get_dx(m),mouse_y_scale*libinput_event_pointer_get_dy(m));frame(d);motions++;
            } else if(t==LIBINPUT_EVENT_POINTER_SCROLL_WHEEL) {
                struct libinput_event_pointer *s=libinput_event_get_pointer_event(e);struct ei_device *d=receiver(EI_DEVICE_CAP_SCROLL);
                double x=0,y=0;
                if(libinput_event_pointer_has_axis(s,LIBINPUT_POINTER_AXIS_SCROLL_HORIZONTAL))x=libinput_event_pointer_get_scroll_value_v120(s,LIBINPUT_POINTER_AXIS_SCROLL_HORIZONTAL);
                if(libinput_event_pointer_has_axis(s,LIBINPUT_POINTER_AXIS_SCROLL_VERTICAL))y=libinput_event_pointer_get_scroll_value_v120(s,LIBINPUT_POINTER_AXIS_SCROLL_VERTICAL);
                ei_device_scroll_discrete(d,(int32_t)x,(int32_t)y);frame(d);scrolls++;
            }
        }
        libinput_event_destroy(e);
    }
}
static int connect_eis(const char *path) {
    int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);if(fd<0)return -1;
    struct sockaddr_un address={.sun_family=AF_UNIX};
    if(strlen(path)>=sizeof address.sun_path){close(fd);errno=ENAMETOOLONG;return -1;}
    strcpy(address.sun_path,path);
    if(connect(fd,(struct sockaddr*)&address,sizeof address)<0){close(fd);return -1;}
    struct ucred peer; socklen_t len=sizeof peer;struct stat st;
    if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&len)<0 || peer.uid!=geteuid() || lstat(path,&st)<0 || !S_ISSOCK(st.st_mode) || st.st_uid!=geteuid()) {close(fd);errno=EPERM;return -1;}
    fprintf(stderr,"USB_EIS_PEER pid=%d uid=%u\n",peer.pid,peer.uid);
    return fd;
}
int main(int argc,char **argv) {
    bool probe=argc==2 && !strcmp(argv[1],"--probe");unsigned int duration=0;
    if(argc==3 && !strcmp(argv[1],"--duration")){char *end;unsigned long n=strtoul(argv[2],&end,10);if(*end || !n || n>3600)return 2;duration=(unsigned int)n;}
    else if(argc!=1 && !probe)return 2;
    const char *invert_y=getenv("STEAM_USB_MOUSE_INVERT_Y");
    if(invert_y && strcmp(invert_y,"0") && strcmp(invert_y,"1")){fprintf(stderr,"USB_MOUSE_INVERT_Y_INVALID\n");return 3;}
    if(invert_y && !strcmp(invert_y,"1"))mouse_y_scale=-1.0;
    const char *runtime=getenv("XDG_RUNTIME_DIR");if(!runtime || runtime[0]!='/')return 3;
    char socket_path[256],lock_path[256];snprintf(socket_path,sizeof socket_path,"%s/gamescope-0-ei",runtime);
    snprintf(lock_path,sizeof lock_path,"%s/steam-usb-input.lock",runtime);
    int lock=open(lock_path,O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW,0600);if(lock<0 || flock(lock,LOCK_EX|LOCK_NB)<0){fprintf(stderr,"USB_BRIDGE_LOCK_FAILED\n");return 4;}
    struct sigaction sa={.sa_handler=stop};sigemptyset(&sa.sa_mask);sigaction(SIGTERM,&sa,NULL);sigaction(SIGINT,&sa,NULL);sigaction(SIGHUP,&sa,NULL);
    fprintf(stderr,"USB_BRIDGE_VARIANT m10n-usb-eis-v3 pid=%d probe=%d invert_y=%d\n",getpid(),probe,mouse_y_scale<0);
    int fd=connect_eis(socket_path);if(fd<0){perror("EIS connect");return 5;}
    ei=ei_new_sender(NULL);if(!ei){close(fd);return 6;}ei_configure_name(ei,"Steam USB keyboard and mouse bridge");
    if(ei_setup_backend_fd(ei,fd)<0){ei_unref(ei);return 7;}
    uint64_t deadline=now_us()+10000000;
    while(!ready() && !disconnected && !stopping && now_us()<deadline){dispatch_ei();struct pollfd f={.fd=ei_get_fd(ei),.events=POLLIN};poll(&f,1,50);}
    int rc=0;
    if(!ready() || disconnected || stopping){fprintf(stderr,"USB_EI_READINESS_FAILED\n");rc=8;goto cleanup;}
    fprintf(stderr,"USB_EI_READY keyboard=1 pointer=1 button=1 scroll=1\n");
    if(probe)goto cleanup;
    udev=udev_new();if(!udev){rc=9;goto cleanup;}
    li=libinput_path_create_context(&input_interface,NULL);if(!li){rc=10;goto cleanup;}
    struct udev_monitor *monitor=udev_monitor_new_from_netlink(udev,"udev");
    if(!monitor || udev_monitor_filter_add_match_subsystem_devtype(monitor,"input",NULL)<0 || udev_monitor_enable_receiving(monitor)<0){if(monitor)udev_monitor_unref(monitor);rc=11;goto cleanup;}
    struct udev_enumerate *scan=udev_enumerate_new(udev);if(!scan){udev_monitor_unref(monitor);rc=12;goto cleanup;}
    udev_enumerate_add_match_subsystem(scan,"input");udev_enumerate_scan_devices(scan);struct udev_list_entry *entry;
    udev_list_entry_foreach(entry,udev_enumerate_get_list_entry(scan)) {struct udev_device *d=udev_device_new_from_syspath(udev,udev_list_entry_get_name(entry));if(d){add_device(d);udev_device_unref(d);}}
    udev_enumerate_unref(scan);dispatch_input();
    uint64_t end=duration?now_us()+(uint64_t)duration*1000000:UINT64_MAX;
    fprintf(stderr,"USB_BRIDGE_READY hotplug=1 exclusive_grab=0\n");
    while(!stopping && !disconnected && now_us()<end) {
        struct pollfd f[]={{.fd=ei_get_fd(ei),.events=POLLIN},{.fd=libinput_get_fd(li),.events=POLLIN},{.fd=udev_monitor_get_fd(monitor),.events=POLLIN}};
        int n=poll(f,3,500);if(n<0){if(errno==EINTR)continue;rc=13;break;}
        dispatch_ei();if(disconnected){rc=14;break;}
        if(f[2].revents&POLLIN) {
            struct udev_device *d;
            while((d=udev_monitor_receive_device(monitor))) {
                const char *action=udev_device_get_action(d),*node=udev_device_get_devnode(d);
                if(action && (!strcmp(action,"add") || !strcmp(action,"change")))add_device(d);
                else if(action && !strcmp(action,"remove") && node)for(int i=0;i<NPHYS;i++)if(phys[i].dev && !phys[i].removing && !strcmp(phys[i].node,node)){release_physical(&phys[i]);phys[i].removing=true;libinput_path_remove_device(phys[i].dev);}
                udev_device_unref(d);
            }
        }
        dispatch_input();
    }
    udev_monitor_unref(monitor);
    if(input_failed && !rc)rc=16;
cleanup:
    if(!disconnected) {
        for(int i=0;i<NPHYS;i++)release_physical(&phys[i]);
        for(int i=0;i<NEI;i++)if(virt[i].dev && virt[i].resumed){ei_device_stop_emulating(virt[i].dev);virt[i].resumed=false;}
        struct ei_ping *ping=ei_new_ping(ei);got_pong=false;ei_ping(ping);deadline=now_us()+500000;
        while(!got_pong && !disconnected && now_us()<deadline){dispatch_ei();struct pollfd f={.fd=ei_get_fd(ei),.events=POLLIN};poll(&f,1,20);}
        ei_ping_unref(ping);
        fprintf(stderr,"USB_EI_CLEANUP roundtrip=%d disconnected=%d\n",got_pong,disconnected);
        if((!got_pong || disconnected) && !rc)rc=15;
    }
    for(int i=0;i<NPHYS;i++)if(phys[i].dev)libinput_device_unref(phys[i].dev);
    if(li)libinput_unref(li);
    if(udev)udev_unref(udev);
    for(int i=0;i<NEI;i++)if(virt[i].dev)ei_device_unref(virt[i].dev);
    ei_unref(ei);close(lock);
    fprintf(stderr,"USB_BRIDGE_EXIT rc=%d motions=%llu keys=%llu buttons=%llu scrolls=%llu\n",rc,(unsigned long long)motions,(unsigned long long)key_events,(unsigned long long)button_events,(unsigned long long)scrolls);
    return rc;
}
