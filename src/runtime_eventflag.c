/* Orbis kernel event flags used by First Light streaming/worker threads.
 * Host implementation with explicit wait/clear semantics; no success stubs. */
#define _GNU_SOURCE
#include "runtime.h"
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define ERR(n) ((int32_t)(UINT32_C(0x80020000)|(n)))
#define ATTR_FIFO 0x01u
#define ATTR_PRIO 0x02u
#define ATTR_SINGLE 0x10u
#define ATTR_MULTI 0x20u
#define WAIT_AND 0x01u
#define WAIT_OR 0x02u
#define CLEAR_ALL 0x10u
#define CLEAR_BITS 0x20u

typedef struct EventFlag {
    pthread_mutex_t lock;
    pthread_cond_t changed;
    pthread_cond_t drained;
    uint64_t bits;
    uint32_t attr;
    uint32_t waiters;
    int deleting;
    char name[32];
} EventFlag;

static size_t created_count, wait_count, set_count, timeout_count;

static int valid_attr(uint32_t attr) {
    const uint32_t queue=attr&0x0f, mode=attr&0xf0;
    return (queue==0 || queue==ATTR_FIFO || queue==ATTR_PRIO) &&
           (mode==0 || mode==ATTR_SINGLE || mode==ATTR_MULTI) &&
           !(attr&~0x33u);
}
static int valid_wait(uint32_t mode) {
    uint32_t test=mode&0x0f, clear=mode&0xf0;
    return (test==WAIT_AND || test==WAIT_OR) &&
           (clear==0 || clear==CLEAR_ALL || clear==CLEAR_BITS) &&
           !(mode&~0x33u);
}
static int condition(uint64_t value,uint64_t pattern,uint32_t mode) {
    return (mode&0x0f)==WAIT_AND ? (value&pattern)==pattern : (value&pattern)!=0;
}
static void apply_clear(EventFlag *e,uint64_t pattern,uint32_t mode) {
    if ((mode&0xf0)==CLEAR_ALL) e->bits=0;
    else if ((mode&0xf0)==CLEAR_BITS) e->bits&=~pattern;
}
static void deadline_after_us(struct timespec *out,uint32_t us) {
    clock_gettime(CLOCK_REALTIME,out);
    uint64_t ns=(uint64_t)out->tv_nsec+(uint64_t)us*1000;
    out->tv_sec+=(time_t)(ns/1000000000);
    out->tv_nsec=(long)(ns%1000000000);
}

static ABI int32_t event_create(EventFlag **out,const char *name,uint32_t attr,
                                uint64_t initial,const void *opt) {
    if (!out || !name || opt || !valid_attr(attr)) return ERR(22);
    if (strlen(name)>=32) return ERR(63);
    EventFlag *e=calloc(1,sizeof(*e));
    if (!e) return ERR(12);
    if (pthread_mutex_init(&e->lock,NULL) ||
        pthread_cond_init(&e->changed,NULL) ||
        pthread_cond_init(&e->drained,NULL)) {
        free(e); return ERR(12);
    }
    e->bits=initial; e->attr=attr;
    memcpy(e->name,name,strlen(name)+1);
    *out=e;
    ++created_count;
    printf("Runtime: event flag '%s' created, attr=0x%x bits=0x%llx\n",
           e->name,attr,(unsigned long long)initial);
    return 0;
}
static ABI int32_t event_delete(EventFlag *e) {
    if (!e) return ERR(3);
    pthread_mutex_lock(&e->lock);
    e->deleting=1;
    pthread_cond_broadcast(&e->changed);
    while (e->waiters) pthread_cond_wait(&e->drained,&e->lock);
    pthread_mutex_unlock(&e->lock);
    pthread_cond_destroy(&e->changed);
    pthread_cond_destroy(&e->drained);
    pthread_mutex_destroy(&e->lock);
    free(e);
    return 0;
}
static ABI int32_t event_set(EventFlag *e,uint64_t bits) {
    if (!e) return ERR(3);
    pthread_mutex_lock(&e->lock);
    if (e->deleting) { pthread_mutex_unlock(&e->lock); return ERR(13); }
    e->bits|=bits;
    ++set_count;
    pthread_cond_broadcast(&e->changed);
    pthread_mutex_unlock(&e->lock);
    return 0;
}
static ABI int32_t event_clear(EventFlag *e,uint64_t mask) {
    if (!e) return ERR(3);
    pthread_mutex_lock(&e->lock);
    e->bits&=mask; /* Orbis clear keeps the bits present in mask. */
    pthread_mutex_unlock(&e->lock);
    return 0;
}
static ABI int32_t event_wait(EventFlag *e,uint64_t pattern,uint32_t mode,
                              uint64_t *result,uint32_t *timeout_us) {
    if (!e) return ERR(3);
    if (!pattern || !valid_wait(mode)) return ERR(22);
    pthread_mutex_lock(&e->lock);
    if (e->deleting) { pthread_mutex_unlock(&e->lock); return ERR(13); }
    if ((e->attr&0xf0)==ATTR_SINGLE && e->waiters) {
        pthread_mutex_unlock(&e->lock); return ERR(1);
    }
    ++e->waiters; ++wait_count;
    int32_t rc=0;
    struct timespec deadline={0};
    if (timeout_us) deadline_after_us(&deadline,*timeout_us);
    while (!e->deleting && !condition(e->bits,pattern,mode)) {
        int r=timeout_us ? pthread_cond_timedwait(&e->changed,&e->lock,&deadline)
                         : pthread_cond_wait(&e->changed,&e->lock);
        if (r==ETIMEDOUT) { rc=ERR(60); ++timeout_count; break; }
        if (r) { rc=ERR(5); break; }
    }
    if (!rc && e->deleting) rc=ERR(13);
    if (result) *result=e->bits;
    if (!rc) apply_clear(e,pattern,mode);
    if (timeout_us) *timeout_us=0;
    if (--e->waiters==0 && e->deleting) pthread_cond_signal(&e->drained);
    pthread_mutex_unlock(&e->lock);
    return rc;
}
static ABI int32_t event_poll(EventFlag *e,uint64_t pattern,uint32_t mode,uint64_t *result) {
    uint32_t zero=0;
    int32_t r=event_wait(e,pattern,mode,result,&zero);
    return r==ERR(60) ? ERR(16) : r;
}
static ABI int32_t event_cancel(EventFlag *e,uint64_t pattern,int32_t *waiters) {
    if (!e) return ERR(3);
    pthread_mutex_lock(&e->lock);
    if (waiters) *waiters=(int32_t)e->waiters;
    e->bits=pattern;
    pthread_cond_broadcast(&e->changed);
    pthread_mutex_unlock(&e->lock);
    return 0;
}

static const RuntimeExport exports[]={
    {"sceKernelCreateEventFlag",event_create},
    {"sceKernelDeleteEventFlag",event_delete},
    {"sceKernelSetEventFlag",event_set},
    {"sceKernelClearEventFlag",event_clear},
    {"sceKernelWaitEventFlag",event_wait},
    {"sceKernelPollEventFlag",event_poll},
    {"sceKernelCancelEventFlag",event_cancel},
};
uintptr_t runtime_eventflag_resolve(const char *name) { return RUNTIME_LOOKUP(exports,name); }
void runtime_eventflag_report(void) {
    printf("Runtime: event flags created=%zu, waits=%zu, sets=%zu, timeouts=%zu\n",
           created_count,wait_count,set_count,timeout_count);
}
