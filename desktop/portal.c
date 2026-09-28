/* xdg-desktop-portal RemoteDesktop + ScreenCast, one consented session per
 * panel. PipeWire receives the portal fd, never the unrestricted daemon fd.
 * Input uses only the devices granted in Start's response. */
#ifndef _WIN32
#include "controller.h"
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <unistd.h>
#include <string.h>
#define BUS "org.freedesktop.portal.Desktop"
#define PATH "/org/freedesktop/portal/desktop"
#define RD "org.freedesktop.portal.RemoteDesktop"
#define SC "org.freedesktop.portal.ScreenCast"
typedef struct {
    GDBusConnection *bus;
    GMainContext *context;
    char *session;
    int fd;
    unsigned node, devices;
    int width,height;
} Portal;
typedef struct {GVariant *result;int done;unsigned code;} Response;
static void response(GDBusConnection *c,const char *sender,const char *path,const char *iface,
                     const char *signal,GVariant *params,gpointer data) {
    (void)c;(void)sender;(void)path;(void)iface;(void)signal;
    Response *r=data;g_variant_get(params,"(u@a{sv})",&r->code,&r->result);r->done=1;
}
static void option(GVariantBuilder *b,const char *key,GVariant *v) {g_variant_builder_add(b,"{sv}",key,v);}
static GVariant *request(Portal *p,const char *iface,const char *method,GVariant *args,
                         const char *token,char *error,int capacity) {
    char *sender=g_strdup(g_dbus_connection_get_unique_name(p->bus)+1);
    for(char *c=sender;*c;c++)if(*c=='.')*c='_';
    char *path=g_strdup_printf(PATH "/request/%s/%s",sender,token);g_free(sender);
    Response r={0};GError *e=NULL;
    guint sub=g_dbus_connection_signal_subscribe(p->bus,BUS,"org.freedesktop.portal.Request","Response",
                                                 path,NULL,G_DBUS_SIGNAL_FLAGS_NONE,response,&r,NULL);
    GVariant *reply=g_dbus_connection_call_sync(p->bus,BUS,PATH,iface,method,args,G_VARIANT_TYPE("(o)"),
                                               G_DBUS_CALL_FLAGS_NONE,10000,NULL,&e);
    if(reply)g_variant_unref(reply);
    int64_t deadline=g_get_monotonic_time()+120000000;
    while(!e && !r.done && g_get_monotonic_time()<deadline) {
        while(g_main_context_iteration(p->context,FALSE)) {}
        g_usleep(10000);
    }
    if(!r.done) {
        GVariant *closed=g_dbus_connection_call_sync(p->bus,BUS,path,"org.freedesktop.portal.Request","Close",
                                NULL,NULL,G_DBUS_CALL_FLAGS_NONE,2000,NULL,NULL);
        if(closed)g_variant_unref(closed);
    }
    g_dbus_connection_signal_unsubscribe(p->bus,sub);g_free(path);
    if(e || !r.done || r.code) {
        g_strlcpy(error,e?e->message:!r.done?"Screen sharing request timed out":"Screen sharing was cancelled or refused",capacity);
        if(e)g_error_free(e);if(r.result)g_variant_unref(r.result);return NULL;
    }
    return r.result;
}
FC_API void fc_portal_close(Portal *p) {
    if(!p)return;
    if(p->session && p->bus) {
        GVariant *r=g_dbus_connection_call_sync(p->bus,BUS,p->session,"org.freedesktop.portal.Session","Close",
                                                NULL,NULL,G_DBUS_CALL_FLAGS_NONE,2000,NULL,NULL);
        if(r)g_variant_unref(r);
    }
    if(p->fd>=0)close(p->fd);
    g_free(p->session);if(p->bus)g_object_unref(p->bus);
    if(p->context)g_main_context_unref(p->context);g_free(p);
}
FC_API Portal *fc_portal_select(char *error,int capacity) {
    Portal *p=g_new0(Portal,1);p->fd=-1;p->context=g_main_context_new();
    g_main_context_push_thread_default(p->context);
    GError *e=NULL;GVariant *r=NULL;
    p->bus=g_bus_get_sync(G_BUS_TYPE_SESSION,NULL,&e);
    if(!p->bus) {g_strlcpy(error,e->message,capacity);g_error_free(e);goto fail;}
    GVariantBuilder b;char token[64],session[64];
    g_snprintf(session,sizeof(session),"fc_%u",g_random_int());
    g_snprintf(token,sizeof(token),"fc_%u",g_random_int());
    g_variant_builder_init(&b,G_VARIANT_TYPE_VARDICT);
    option(&b,"handle_token",g_variant_new_string(token));option(&b,"session_handle_token",g_variant_new_string(session));
    r=request(p,RD,"CreateSession",g_variant_new("(a{sv})",&b),token,error,capacity);if(!r)goto fail;
    g_variant_lookup(r,"session_handle","s",&p->session);g_variant_unref(r);r=NULL;
    if(!p->session){g_strlcpy(error,"Portal did not return a session",capacity);goto fail;}
    g_snprintf(token,sizeof(token),"fc_%u",g_random_int());g_variant_builder_init(&b,G_VARIANT_TYPE_VARDICT);
    option(&b,"handle_token",g_variant_new_string(token));option(&b,"types",g_variant_new_uint32(3));
    r=request(p,RD,"SelectDevices",g_variant_new("(oa{sv})",p->session,&b),token,error,capacity);if(!r)goto fail;g_variant_unref(r);
    g_snprintf(token,sizeof(token),"fc_%u",g_random_int());g_variant_builder_init(&b,G_VARIANT_TYPE_VARDICT);
    option(&b,"handle_token",g_variant_new_string(token));option(&b,"types",g_variant_new_uint32(3));
    option(&b,"multiple",g_variant_new_boolean(FALSE));option(&b,"cursor_mode",g_variant_new_uint32(2));
    r=request(p,SC,"SelectSources",g_variant_new("(oa{sv})",p->session,&b),token,error,capacity);if(!r)goto fail;g_variant_unref(r);
    g_snprintf(token,sizeof(token),"fc_%u",g_random_int());g_variant_builder_init(&b,G_VARIANT_TYPE_VARDICT);
    option(&b,"handle_token",g_variant_new_string(token));
    r=request(p,RD,"Start",g_variant_new("(osa{sv})",p->session,"",&b),token,error,capacity);if(!r)goto fail;
    g_variant_lookup(r,"devices","u",&p->devices);
    GVariant *streams=g_variant_lookup_value(r,"streams",G_VARIANT_TYPE("a(ua{sv})"));
    if(streams && g_variant_n_children(streams)>0) {
        GVariant *entry=g_variant_get_child_value(streams,0),*props=NULL;
        g_variant_get(entry,"(u@a{sv})",&p->node,&props);
        g_variant_lookup(props,"size","(ii)",&p->width,&p->height);
        g_variant_unref(props);g_variant_unref(entry);
    }
    if(streams)g_variant_unref(streams);g_variant_unref(r);r=NULL;
    if(!p->node || p->width<=0 || p->height<=0) {g_strlcpy(error,"Portal returned no stream size",capacity);goto fail;}
    g_variant_builder_init(&b,G_VARIANT_TYPE_VARDICT);GUnixFDList *fds=NULL;
    r=g_dbus_connection_call_with_unix_fd_list_sync(p->bus,BUS,PATH,SC,"OpenPipeWireRemote",
         g_variant_new("(oa{sv})",p->session,&b),G_VARIANT_TYPE("(h)"),G_DBUS_CALL_FLAGS_NONE,10000,NULL,&fds,NULL,&e);
    if(!r){g_strlcpy(error,e->message,capacity);g_error_free(e);goto fail;}
    int handle;g_variant_get(r,"(h)",&handle);g_variant_unref(r);
    p->fd=g_unix_fd_list_get(fds,handle,&e);g_object_unref(fds);
    if(p->fd<0){g_strlcpy(error,e->message,capacity);g_error_free(e);goto fail;}
    g_main_context_pop_thread_default(p->context);return p;
fail:
    g_main_context_pop_thread_default(p->context);fc_portal_close(p);return NULL;
}
FC_API int fc_portal_value(Portal *p,int field) {
    switch(field){case 0:return p->fd;case 1:return (int)p->node;case 2:return p->width;
                 case 3:return p->height;case 4:return (int)p->devices;default:return 0;}
}
FC_API int fc_portal_input(Portal *p,int type,double x,double y,int code,int down) {
    const char *method=NULL;GVariant *args=NULL;GVariantBuilder b;g_variant_builder_init(&b,G_VARIANT_TYPE_VARDICT);
    if(type==0 && (p->devices&2)) {
        method="NotifyPointerMotionAbsolute";args=g_variant_new("(oa{sv}udd)",p->session,&b,p->node,x,y);
    } else if(type==1 && (p->devices&2)) {
        method="NotifyPointerButton";args=g_variant_new("(oa{sv}iu)",p->session,&b,code,(guint)down);
    } else if(type==2 && (p->devices&2)) {
        method="NotifyPointerAxis";args=g_variant_new("(oa{sv}dd)",p->session,&b,x,y);
    } else if(type==3 && (p->devices&1)) {
        method="NotifyKeyboardKeysym";args=g_variant_new("(oa{sv}iu)",p->session,&b,code,(guint)down);
    } else return 0;
    GError *e=NULL;GVariant *r=g_dbus_connection_call_sync(p->bus,BUS,PATH,RD,method,args,NULL,
                                                       G_DBUS_CALL_FLAGS_NONE,2000,NULL,&e);
    if(e)g_error_free(e);if(r)g_variant_unref(r);return r!=NULL;
}
#endif
