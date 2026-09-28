/* Frame Control's capture/encode adapter. GStreamer is a bundled library;
 * capture uses WGC on Windows and the consented PipeWire fd on Linux. */
#include "controller.h"
#include <gst/gst.h>
#include <gst/app/gstappsink.h>
#include <gst/video/video-event.h>
#include <string.h>

typedef int (*Gate)(int stage, int64_t pts, int64_t capture, int64_t arrived);
typedef struct {
    GstElement *pipeline, *encoder, *sink;
    Gate gate;
    GstSample *sample;
    GstMapInfo map;
    int mapped;
    char error[512];
} Capture;
typedef struct { const unsigned char *data; int size, key, width, height; int64_t pts; } Encoded;
FC_API int64_t fc_now(void) {return g_get_monotonic_time();}
FC_API void fc_gst_init(void) {gst_init(NULL,NULL);}
FC_API int fc_has_element(const char *name) {
    GstElementFactory *f=gst_element_factory_find(name);
    if(!f)return 0;gst_object_unref(f);return 1;
}
static GstPadProbeReturn probe(GstPad *pad,GstPadProbeInfo *info,gpointer data) {
    Capture *c=data; GstBuffer *b=GST_PAD_PROBE_INFO_BUFFER(info);
    if(!b)return GST_PAD_PROBE_OK;
    int64_t now=fc_now(), cap=now;
    GstClock *clock=gst_element_get_clock(c->pipeline);
    if(clock && GST_BUFFER_PTS_IS_VALID(b)) {
        GstClockTime current=gst_clock_get_time(clock),origin=gst_element_get_base_time(c->pipeline);
        if(current>=origin && current-origin>=GST_BUFFER_PTS(b))
            cap-= (int64_t)((current-origin-GST_BUFFER_PTS(b))/1000);
    }
    if(clock)gst_object_unref(clock);
    int stage=GPOINTER_TO_INT(g_object_get_data(G_OBJECT(pad),"stage"));
    return c->gate(stage,(int64_t)GST_BUFFER_PTS(b),cap,now) ? GST_PAD_PROBE_OK : GST_PAD_PROBE_DROP;
}
FC_API Capture *fc_capture_open(const char *pipeline,Gate gate,char *error,int capacity) {
    GError *e=NULL;Capture *c=g_new0(Capture,1);c->gate=gate;
    c->pipeline=gst_parse_launch(pipeline,&e);
    if(e || !c->pipeline) {
        g_strlcpy(error,e?e->message:"No pipeline",capacity);
        if(e)g_error_free(e);if(c->pipeline)gst_object_unref(c->pipeline);g_free(c);return NULL;
    }
    c->encoder=gst_bin_get_by_name(GST_BIN(c->pipeline),"enc");
    c->sink=gst_bin_get_by_name(GST_BIN(c->pipeline),"out");
    GstElement *raw=gst_bin_get_by_name(GST_BIN(c->pipeline),"gate");
    if(!c->encoder || !c->sink || !raw) {
        g_strlcpy(error,"Pipeline is missing enc, out or gate",capacity);
        if(raw)gst_object_unref(raw);if(c->encoder)gst_object_unref(c->encoder);
        if(c->sink)gst_object_unref(c->sink);gst_object_unref(c->pipeline);g_free(c);return NULL;
    }
    GstPad *p=gst_element_get_static_pad(raw,"src");
    g_object_set_data(G_OBJECT(p),"stage",GINT_TO_POINTER(0));
    gst_pad_add_probe(p,GST_PAD_PROBE_TYPE_BUFFER,probe,c,NULL);gst_object_unref(p);gst_object_unref(raw);
    p=gst_element_get_static_pad(c->encoder,"sink");
    g_object_set_data(G_OBJECT(p),"stage",GINT_TO_POINTER(1));
    gst_pad_add_probe(p,GST_PAD_PROBE_TYPE_BUFFER,probe,c,NULL);gst_object_unref(p);
    gst_element_set_state(c->pipeline,GST_STATE_PLAYING);
    return c;
}
FC_API int fc_capture_pull(Capture *c,Encoded *out) {
    if(c->mapped) {gst_buffer_unmap(gst_sample_get_buffer(c->sample),&c->map);c->mapped=0;}
    if(c->sample){gst_sample_unref(c->sample);c->sample=NULL;}
    GstBus *bus=gst_element_get_bus(c->pipeline);
    GstMessage *m=gst_bus_pop_filtered(bus,GST_MESSAGE_ERROR|GST_MESSAGE_EOS);gst_object_unref(bus);
    if(m) {
        if(GST_MESSAGE_TYPE(m)==GST_MESSAGE_ERROR) {
            GError *e=NULL;char *debug=NULL;gst_message_parse_error(m,&e,&debug);
            g_strlcpy(c->error,e->message,sizeof(c->error));g_error_free(e);g_free(debug);
        } else g_strlcpy(c->error,"The capture source closed",sizeof(c->error));
        gst_message_unref(m);return -1;
    }
    c->sample=gst_app_sink_try_pull_sample(GST_APP_SINK(c->sink),100*GST_MSECOND);
    if(!c->sample)return 0;
    GstBuffer *b=gst_sample_get_buffer(c->sample);
    if(!gst_buffer_map(b,&c->map,GST_MAP_READ))return 0;
    c->mapped=1;out->data=c->map.data;out->size=(int)c->map.size;
    out->key=!GST_BUFFER_FLAG_IS_SET(b,GST_BUFFER_FLAG_DELTA_UNIT);out->pts=(int64_t)GST_BUFFER_PTS(b);
    const GstStructure *s=gst_caps_get_structure(gst_sample_get_caps(c->sample),0);
    gst_structure_get_int(s,"width",&out->width);gst_structure_get_int(s,"height",&out->height);return 1;
}
FC_API const char *fc_capture_error(Capture *c) {return c->error;}
FC_API void fc_capture_bitrate(Capture *c,int bps) {
    g_object_set(c->encoder,"bitrate",(guint)MAX(1,bps/1000),NULL);
}
FC_API void fc_capture_test(Capture *c,uint32_t input) {
    GstElement *source=gst_bin_get_by_name(GST_BIN(c->pipeline),"source");
    if(source) {
        /* Each benchmark click visibly changes the ball, before injection is
         * timestamped. This is a response, not merely a protocol input echo. */
        guint color=0xff000000u | ((input*2654435761u)&0x00ffffffu);
        g_object_set(source,"foreground-color",color,NULL);gst_object_unref(source);
    }
}
FC_API void fc_capture_key(Capture *c) {
    GstPad *p=gst_element_get_static_pad(c->encoder,"src");
    gst_pad_send_event(p,gst_video_event_new_upstream_force_key_unit(GST_CLOCK_TIME_NONE,TRUE,0));gst_object_unref(p);
}
FC_API void fc_capture_close(Capture *c) {
    gst_element_set_state(c->pipeline,GST_STATE_NULL);
    if(c->mapped)gst_buffer_unmap(gst_sample_get_buffer(c->sample),&c->map);
    if(c->sample)gst_sample_unref(c->sample);
    gst_object_unref(c->sink);gst_object_unref(c->encoder);gst_object_unref(c->pipeline);g_free(c);
}
