#include <stdio.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/input.h>
/* Included in touch_inject.routed.c. No renderer, focus or stacking changes. */
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/Xatom.h>
#include <X11/extensions/shape.h>
#include <signal.h>
#include <string.h>
#include <stdlib.h>
#include <errno.h>
#include <poll.h>

static volatile sig_atomic_t stopping;
static struct { Window window; unsigned long pid; } owned_routes[8];
static void stop_touch(int n) { (void)n; stopping=1; }
static int routing_xerror(Display *d, XErrorEvent *e) {
    (void)d;
    fprintf(stderr,"TOUCH_XERROR code=%u request=%u resource=%lx\n",e->error_code,e->request_code,e->resourceid);
    return 0;
}
static unsigned long cardinal(Display *d, Window w, const char *name) {
    Atom type; int format; unsigned long count,rest,value=0; unsigned char *data=NULL;
    if(XGetWindowProperty(d,w,XInternAtom(d,name,False),0,1,False,XA_CARDINAL,
                         &type,&format,&count,&rest,&data)==Success &&
       type==XA_CARDINAL && format==32 && count==1 && data) value=((unsigned long*)data)[0];
    if(data)XFree(data);
    return value;
}
static int steam_proxy(Display *d, Window w, unsigned long *pid) {
    XWindowAttributes a; XClassHint c={0}; int match=0;
    if(!w || !XGetWindowAttributes(d,w,&a) || a.map_state!=IsViewable ||
       a.class!=InputOutput || (a.all_event_masks&ButtonPressMask) ||
       cardinal(d,w,"STEAM_GAME")!=769) return 0;
    if(XGetClassHint(d,w,&c))
        match=c.res_name && c.res_class && !strcmp(c.res_name,"steamwebhelper") && !strcmp(c.res_class,"steam");
    if(c.res_name)XFree(c.res_name);
    if(c.res_class)XFree(c.res_class);
    *pid=cardinal(d,w,"_NET_WM_PID");
    return match && *pid;
}
static int has_cef_receiver(Display *d, Window parent, unsigned long pid, int depth) {
    Window root,up,*children=NULL; unsigned int count=0; int found=0;
    if(!XQueryTree(d,parent,&root,&up,&children,&count))return 0;
    for(unsigned int i=0;i<count && !found;i++) {
        XWindowAttributes a;
        if(!XGetWindowAttributes(d,children[i],&a) || a.map_state!=IsViewable)continue;
        found=(a.width>1 && a.height>1 &&
               (a.all_event_masks&(ButtonPressMask|ButtonReleaseMask))==(ButtonPressMask|ButtonReleaseMask) &&
               cardinal(d,children[i],"STEAM_GAME")==769 && cardinal(d,children[i],"_NET_WM_PID")==pid);
        if(!found && depth>0)found=has_cef_receiver(d,children[i],pid,depth-1);
    }
    if(children)XFree(children);
    return found;
}
static int input_shape_empty(Display *d, Window w) {
    int count=0,ordering=0;
    XRectangle *r=XShapeGetRectangles(d,w,ShapeInput,&count,&ordering);
    if(r)XFree(r);
    return count==0;
}
static void route_cef_pointer(Display *d) {
    Window root=DefaultRootWindow(d);
    if(cardinal(d,root,"GAMESCOPE_FOCUSED_APP")!=769)return;
    Window w=cardinal(d,root,"GAMESCOPE_FOCUSED_WINDOW"); unsigned long pid=0;
    if(!steam_proxy(d,w,&pid))return;
    for(unsigned int i=0;i<sizeof owned_routes/sizeof *owned_routes;i++)
        if(owned_routes[i].window==w && owned_routes[i].pid==pid && input_shape_empty(d,w))return;
    if(!has_cef_receiver(d,root,pid,1))return;
    /* Only replace a full-window input region. Never erase custom hit regions. */
    XWindowAttributes a;
    if(!XGetWindowAttributes(d,w,&a))return;
    int count=0,ordering=0; XRectangle *r=XShapeGetRectangles(d,w,ShapeInput,&count,&ordering);
    int full=count==1 && r && r[0].x==0 && r[0].y==0 && r[0].width==a.width && r[0].height==a.height;
    if(r)XFree(r);
    if(!full)return;
    int slot=-1;
    for(unsigned int i=0;i<sizeof owned_routes/sizeof *owned_routes;i++) {
        if(owned_routes[i].window==w && owned_routes[i].pid==pid){slot=(int)i;break;}
        if(!owned_routes[i].window){slot=(int)i;break;}
        XWindowAttributes old;
        if(!XGetWindowAttributes(d,owned_routes[i].window,&old) ||
           cardinal(d,owned_routes[i].window,"_NET_WM_PID")!=owned_routes[i].pid){slot=(int)i;break;}
    }
    if(slot<0)return;
    XShapeCombineRectangles(d,w,ShapeInput,0,0,NULL,0,ShapeSet,Unsorted);
    XSync(d,False);
    if(!input_shape_empty(d,w))return;
    owned_routes[slot].window=w;owned_routes[slot].pid=pid;
    fprintf(stderr,"TOUCH_CEF_ROUTE proxy=0x%lx owner_pid=%lu input_shape=empty\n",w,pid);
}
static void restore_cef_routes(Display *d) {
    for(unsigned int i=0;i<sizeof owned_routes/sizeof *owned_routes;i++) {
        Window w=owned_routes[i].window; unsigned long pid=0;
        if(w && steam_proxy(d,w,&pid) && pid==owned_routes[i].pid && input_shape_empty(d,w)) {
            XShapeCombineMask(d,w,ShapeInput,0,0,None,ShapeSet);
            fprintf(stderr,"TOUCH_CEF_RESTORE proxy=0x%lx\n",w);
        }
    }
    XSync(d,False);
}

extern int XTestFakeMotionEvent(Display *, int, int, int, unsigned long);
extern int XTestFakeButtonEvent(Display *, unsigned int, int, unsigned long);
static int ar(int fd, int ax, int *mn, int *mx){struct input_absinfo i; if(ioctl(fd,EVIOCGABS(ax),&i)<0) return -1; *mn=i.minimum;*mx=i.maximum; return 0;}
int main(int c, char **v){
    const char *dev = c>1 ? v[1] : "/dev/input/by-path/platform-7000c500.i2c-event";
    Display *d = XOpenDisplay(NULL); if(!d){fprintf(stderr,"no display\n");return 1;}
    XSetErrorHandler(routing_xerror);
    struct sigaction sa={0}; sa.sa_handler=stop_touch; sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM,&sa,NULL); sigaction(SIGINT,&sa,NULL); sigaction(SIGHUP,&sa,NULL);
    int s = XDefaultScreen(d), W = XDisplayWidth(d,s), H = XDisplayHeight(d,s);
    int fd = open(dev, O_RDONLY); if(fd<0){perror("open");return 1;}
    char name[256]={0};
    if(ioctl(fd,EVIOCGNAME(sizeof name),name)<0 || strcmp(name,"touchscreen")){fprintf(stderr,"wrong touch device\n");return 2;}
    int shape_event,shape_error;
    if(!XShapeQueryExtension(d,&shape_event,&shape_error)){fprintf(stderr,"no X shape extension\n");return 3;}
    int xmn=0,xmx=0,ymn=0,ymx=0;
    ar(fd,ABS_MT_POSITION_X,&xmn,&xmx); ar(fd,ABS_MT_POSITION_Y,&ymn,&ymx);
    if(xmx<=xmn) ar(fd,ABS_X,&xmn,&xmx);
    if(ymx<=ymn) ar(fd,ABS_Y,&ymn,&ymx);
    if(xmx<=xmn){xmn=0;xmx=4095;} if(ymx<=ymn){ymn=0;ymx=4095;}
    fprintf(stderr,"screen %dx%d axes x[%d..%d] y[%d..%d]\n",W,H,xmn,xmx,ymn,ymx);
    fprintf(stderr,"TOUCH_VARIANT m10n-cef-routing-events-v2\n");
    Atom focused_app=XInternAtom(d,"GAMESCOPE_FOCUSED_APP",False);
    Atom focused_window=XInternAtom(d,"GAMESCOPE_FOCUSED_WINDOW",False);
    XSelectInput(d,DefaultRootWindow(d),PropertyChangeMask|SubstructureNotifyMask);
    fprintf(stderr,"TOUCH_ROUTING_EVENTS enabled=1 no_periodic_window_scan=1\n");
    route_cef_pointer(d);
    int x=xmn,y=ymn,down=0;
    struct input_event e;
    while(!stopping){
        int reroute=0;
        while(XPending(d)) {
            XEvent event;XNextEvent(d,&event);
            if(event.type==MapNotify || event.type==ReparentNotify ||
               (event.type==PropertyNotify &&
                (event.xproperty.atom==focused_app || event.xproperty.atom==focused_window))) reroute=1;
        }
        if(reroute)route_cef_pointer(d);
        struct pollfd pfd[2]={{.fd=fd,.events=POLLIN},{.fd=ConnectionNumber(d),.events=POLLIN}};
        int ready=poll(pfd,2,500);
        if(ready<0 && errno==EINTR)continue;
        if(ready<0)break;
        if(!ready)continue;
        if(pfd[1].revents&(POLLERR|POLLHUP|POLLNVAL))break;
        if(!(pfd[0].revents&POLLIN)) {
            if(pfd[0].revents&(POLLERR|POLLHUP|POLLNVAL))break;
            continue;
        }
        ssize_t got=read(fd,&e,sizeof e);
        if(got<0 && errno==EINTR)continue;
        if(got!=(ssize_t)sizeof e)break;
        if(e.type==EV_ABS){
            if(e.code==ABS_X||e.code==ABS_MT_POSITION_X) x=e.value;
            else if(e.code==ABS_Y||e.code==ABS_MT_POSITION_Y) y=e.value;
            else if(e.code==ABS_MT_TRACKING_ID){
                int nd = e.value>=0;
                if(nd!=down){if(nd)route_cef_pointer(d);int X=(W-1)-(xmx-x)*(W-1)/(xmx-xmn),Y=(H-1)-(ymx-y)*(H-1)/(ymx-ymn);
                    XTestFakeMotionEvent(d,s,X,Y,0); XTestFakeButtonEvent(d,1,nd,0); XFlush(d); down=nd;}
            }
        } else if(e.type==EV_KEY && (e.code==BTN_TOUCH||e.code==BTN_LEFT)){
            int nd = e.value?1:0;
            if(nd!=down){if(nd)route_cef_pointer(d);int X=(W-1)-(xmx-x)*(W-1)/(xmx-xmn),Y=(H-1)-(ymx-y)*(H-1)/(ymx-ymn);
                XTestFakeMotionEvent(d,s,X,Y,0); XTestFakeButtonEvent(d,1,nd,0); XFlush(d); down=nd;}
        } else if(e.type==EV_SYN && down){
            int X=(W-1)-(xmx-x)*(W-1)/(xmx-xmn),Y=(H-1)-(ymx-y)*(H-1)/(ymx-ymn);
            XTestFakeMotionEvent(d,s,X,Y,0); XFlush(d);
        }
    }
    if(down)XTestFakeButtonEvent(d,1,False,0);
    restore_cef_routes(d);
    close(fd); XCloseDisplay(d);
    return 0;
}
