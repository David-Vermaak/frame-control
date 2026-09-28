/* Extracted from the Mac agent's RateController. Same gate, demand protection,
 * bitrate recovery and tier hysteresis for every host. Bounded sample buffers. */
#include "controller.h"
#include <stdlib.h>
#include <string.h>
#define CAP 1024
#define MIN(a,b) ((a)<(b)?(a):(b))
#define MAX(a,b) ((a)>(b)?(a):(b))
typedef struct { int64_t t, v; uint32_t seq; } Sample;
typedef struct { Sample a[CAP]; int n; } Samples;
struct FCController {
    int max_fps, enabled, ceiling, target, tier, frame_bytes, held, signal, saw_ack;
    int64_t decrease, increase, below, above, slack;
    Samples flight, rtts, acked, sent, captures;
};
static const int fps[] = {60,45,30,30,30}, scale[] = {100,100,100,75,50};
static const double floors[] = {.45,.28,.16,.08,0};
static void remove_first(Samples *s, int n) {
    s->n -= n; memmove(s->a, s->a+n, (size_t)s->n*sizeof(Sample));
}
static void add(Samples *s, Sample v, int cap) {
    if (s->n >= cap) remove_first(s, 1);
    s->a[s->n++] = v;
}
static void expire(Samples *s, int64_t oldest) {
    int n=0; while(n<s->n && s->a[n].t<oldest) n++;
    remove_first(s,n);
}
static int64_t base(FCController *c) {
    int64_t v=0;
    for(int i=0;i<c->rtts.n;i++) if(!i || c->rtts.a[i].v<v) v=c->rtts.a[i].v;
    return v;
}
static int compare(const void *a, const void *b) {
    int64_t x=*(const int64_t*)a, y=*(const int64_t*)b;
    return (x>y)-(x<y);
}
static int64_t quantile(Samples *s,int64_t since,int numerator,int denominator) {
    int64_t a[CAP]; int n=0;
    for(int i=0;i<s->n;i++) if(s->a[i].t>since) a[n++]=s->a[i].v;
    if(!n) return 0;
    qsort(a,(size_t)n,sizeof(int64_t),compare);
    return a[n*numerator/denominator];
}
static int rate(Samples *s) {
    int64_t total=0; for(int i=0;i<s->n;i++) total+=s->a[i].v;
    return (int)MIN(total*16,2147483647);
}
FCController *fc_new(int max_fps,int enabled) {
    FCController *c=calloc(1,sizeof(*c));
    if(c) {c->max_fps=MAX(1,max_fps);c->enabled=enabled;c->slack=40000;}
    return c;
}
void fc_free(FCController *c) {free(c);}
void fc_ceiling(FCController *c,int bps) {
    if(!c->target || c->target>bps) c->target=bps;
    c->ceiling=bps;
}
int fc_gate(FCController *c,int64_t now,int counts) {
    if(!c->enabled || !c->saw_ack) return 1;
    expire(&c->flight,now-2000000);
    if(!c->flight.n) return 1;
    int64_t interval=1000000/MIN(c->max_fps,fps[c->tier]);
    int window=MAX(3,(int)((base(c)+c->slack)/interval)+1);
    if(c->flight.n<window && now-c->flight.a[0].t<=base(c)+c->slack) return 1;
    if(counts) c->held++;
    return 0;
}
void fc_capture(FCController *c,int64_t now) {add(&c->captures,(Sample){now,0,0},256);}
void fc_sent(FCController *c,uint32_t seq,int bytes,int64_t now) {
    Sample s={now,bytes,seq};add(&c->flight,s,512);add(&c->sent,s,CAP);
}
int fc_ack(FCController *c,uint32_t seq,int64_t now) {
    c->saw_ack=1;
    for(int i=0;i<c->flight.n;i++) if(c->flight.a[i].seq==seq) {
        Sample s=c->flight.a[i];remove_first(&c->flight,i+1);
        add(&c->rtts,(Sample){now,now-s.t,0},CAP);
        add(&c->acked,(Sample){now,s.v,0},CAP);return 1;
    }
    return 0;
}
int fc_update(FCController *c,int64_t now) {
    expire(&c->rtts,now-10000000);expire(&c->acked,now-500000);
    expire(&c->sent,now-500000);expire(&c->flight,now-2000000);
    expire(&c->captures,now-1000000);
    if(!c->enabled || c->ceiling<=0) return 0;
    int64_t baseline=base(c), spread=quantile(&c->rtts,now-2000000,9,10);
    int64_t jitter=spread ? spread-baseline : 0;
    c->slack=1000000/MIN(c->max_fps,fps[c->tier])+MIN(MAX(jitter*3/2,25000),80000);
    int64_t recent=quantile(&c->rtts,now-300000,1,2);
    int64_t queue=recent ? recent-baseline : 0;
    int64_t age=c->flight.n ? now-c->flight.a[0].t : 0;
    int delivered=rate(&c->acked),sending=rate(&c->sent);
    if(c->sent.n) c->frame_bytes=sending/16/c->sent.n;
    int demand=(int)MIN((int64_t)MIN(c->captures.n,MIN(c->max_fps,fps[c->tier]))*c->frame_bytes*8,2147483647);
    int signal=(sending>=c->target/2 && queue>40000)||c->held>=3||age>baseline+100000;
    int congested=signal && c->signal;c->signal=signal;c->held=0;
    if(congested && now-c->decrease>300000) {
        int next=MAX(300000,MIN((int64_t)c->target*4/5,MAX((int64_t)delivered*9/10,c->target/2)));
        if(demand>0 && (int64_t)demand*2<=(int64_t)c->target*5/4) next=MAX(next,MIN(c->target,(int64_t)demand*2));
        c->target=next;c->decrease=now;
    } else if(!congested && now-c->decrease>1000000 && now-c->increase>250000 && c->target<c->ceiling &&
              (sending>(int64_t)c->target*6/10 || now-c->decrease>3000000)) {
        c->target=MIN(c->ceiling,(int64_t)(c->target*1.1)+50000);c->increase=now;
    }
    double share=(double)c->target/MAX(c->ceiling,1);
    if(c->tier<4 && share<floors[c->tier] && sending>=(int64_t)c->target*7/10) {
        if(!c->below)c->below=now;
        if(now-c->below>500000) {
            for(c->tier=0;c->tier<4 && share<floors[c->tier];c->tier++) {}
            c->below=0;
        }
    } else c->below=0;
    if(c->tier>0 && share>floors[c->tier-1]*1.25) {
        if(!c->above)c->above=now;
        if(now-c->above>2000000) {c->tier--;c->above=0;}
    } else c->above=0;
    return c->target;
}
int64_t fc_value(FCController *c,int field) {
    switch(field) {
    case 0:return c->target;case 1:return c->ceiling;case 2:return c->tier;
    case 3:return MIN(c->max_fps,fps[c->tier]);case 4:return scale[c->tier];
    case 5:return base(c);case 6:return c->flight.n;case 7:return c->slack;
    default:return 0;
    }
}
