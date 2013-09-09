/* GStreamer
 *
 * Copyright (C) 2013 Collabora Ltd.
 *  Author: Thiago Sousa Santos <thiago.sousa.santos@collabora.com>
 *
 * gst-validate-bin-monitor.c - Validate BinMonitor class
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Library General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Library General Public License for more details.
 *
 * You should have received a copy of the GNU Library General Public
 * License along with this library; if not, write to the
 * Free Software Foundation, Inc., 59 Temple Place - Suite 330,
 * Boston, MA 02111-1307, USA.
 */

#ifdef HAVE_CONFIG_H
#  include "config.h"
#endif

#include "gst-validate-internal.h"
#include "gst-validate-bin-monitor.h"
#include "gst-validate-monitor-factory.h"

/**
 * SECTION:gst-validate-bin-monitor
 * @short_description: Class that wraps a #GstBin for Validate checks
 *
 * TODO
 */

#define gst_validate_bin_monitor_parent_class parent_class
G_DEFINE_TYPE (GstValidateBinMonitor, gst_validate_bin_monitor,
    GST_TYPE_VALIDATE_ELEMENT_MONITOR);

static void
gst_validate_bin_monitor_wrap_element (GstValidateBinMonitor * monitor,
    GstElement * element);
static gboolean gst_validate_bin_monitor_setup (GstValidateMonitor * monitor);

static void
_validate_bin_element_added (GstBin * bin, GstElement * pad,
    GstValidateBinMonitor * monitor);

static void
gst_validate_bin_monitor_dispose (GObject * object)
{
  GstValidateBinMonitor *monitor = GST_VALIDATE_BIN_MONITOR_CAST (object);
  GstElement *bin = GST_VALIDATE_ELEMENT_MONITOR_GET_ELEMENT (monitor);

  if (bin && monitor->element_added_id)
    g_signal_handler_disconnect (bin, monitor->element_added_id);

  if (monitor->scenario)
    g_object_unref (monitor->scenario);

  g_list_free_full (monitor->element_monitors, g_object_unref);

  G_OBJECT_CLASS (parent_class)->dispose (object);
}


static void
gst_validate_bin_monitor_class_init (GstValidateBinMonitorClass * klass)
{
  GObjectClass *gobject_class;
  GstValidateMonitorClass *validatemonitor_class;

  gobject_class = G_OBJECT_CLASS (klass);
  validatemonitor_class = GST_VALIDATE_MONITOR_CLASS_CAST (klass);

  gobject_class->dispose = gst_validate_bin_monitor_dispose;

  validatemonitor_class->setup = gst_validate_bin_monitor_setup;
}

static void
gst_validate_bin_monitor_init (GstValidateBinMonitor * bin_monitor)
{
}

static void
gst_validate_bin_monitor_create_scenarios (GstValidateBinMonitor * monitor)
{
  /* scenarios currently only make sense for pipelines */
  if (GST_IS_PIPELINE (GST_VALIDATE_MONITOR_GET_OBJECT (monitor))) {
    const gchar *scenario_name;

    if ((scenario_name = g_getenv ("GST_VALIDATE_SCENARIO"))) {
      gchar **scenario_v = g_strsplit (scenario_name, ":", 2);

      if (scenario_v[1] && GST_VALIDATE_MONITOR_GET_OBJECT (monitor)) {
        if (!g_pattern_match_simple (scenario_v[1],
                GST_OBJECT_NAME (GST_VALIDATE_MONITOR_GET_OBJECT (monitor)))) {
          GST_INFO_OBJECT (monitor, "Not attaching to bin %" GST_PTR_FORMAT
              " as not matching pattern %s",
              GST_VALIDATE_MONITOR_GET_OBJECT (monitor), scenario_v[1]);

          g_strfreev (scenario_v);
          return;
        }
      }
      monitor->scenario =
          gst_validate_scenario_factory_create (GST_VALIDATE_MONITOR_GET_RUNNER
          (monitor),
          GST_ELEMENT_CAST (GST_VALIDATE_MONITOR_GET_OBJECT (monitor)),
          scenario_v[0]);
      g_strfreev (scenario_v);
    }
  }
}

/**
 * gst_validate_bin_monitor_new:
 * @bin: (transfer-none): a #GstBin to run Validate on
 */
GstValidateBinMonitor *
gst_validate_bin_monitor_new (GstBin * bin, GstValidateRunner * runner,
    GstValidateMonitor * parent)
{
  GstValidateBinMonitor *monitor =
      g_object_new (GST_TYPE_VALIDATE_BIN_MONITOR, "object",
      bin, "validate-runner", runner, "validate-parent", parent, NULL);

  if (GST_VALIDATE_MONITOR_GET_OBJECT (monitor) == NULL) {
    g_object_unref (monitor);
    return NULL;
  }

  gst_validate_bin_monitor_create_scenarios (monitor);

  return monitor;
}

static gboolean
gst_validate_bin_monitor_setup (GstValidateMonitor * monitor)
{
  GstIterator *iterator;
  gboolean done;
  GstElement *element;
  GstValidateBinMonitor *bin_monitor = GST_VALIDATE_BIN_MONITOR_CAST (monitor);
  GstBin *bin = GST_VALIDATE_BIN_MONITOR_GET_BIN (bin_monitor);

  if (!GST_IS_BIN (bin)) {
    GST_WARNING_OBJECT (monitor, "Trying to create bin monitor with other "
        "type of object");
    return FALSE;
  }

  GST_DEBUG_OBJECT (bin_monitor, "Setting up monitor for bin %" GST_PTR_FORMAT,
      bin);

  bin_monitor->element_added_id =
      g_signal_connect (bin, "element-added",
      G_CALLBACK (_validate_bin_element_added), monitor);

  iterator = gst_bin_iterate_elements (bin);
  done = FALSE;
  while (!done) {
    switch (gst_iterator_next (iterator, (gpointer *) & element)) {
      case GST_ITERATOR_OK:
        gst_validate_bin_monitor_wrap_element (bin_monitor, element);
        gst_object_unref (element);
        break;
      case GST_ITERATOR_RESYNC:
        /* TODO how to handle this? */
        gst_iterator_resync (iterator);
        break;
      case GST_ITERATOR_ERROR:
        done = TRUE;
        break;
      case GST_ITERATOR_DONE:
        done = TRUE;
        break;
    }
  }
  gst_iterator_free (iterator);

  return TRUE;
}

static void
gst_validate_bin_monitor_wrap_element (GstValidateBinMonitor * monitor,
    GstElement * element)
{
  GstValidateElementMonitor *element_monitor;
  GST_DEBUG_OBJECT (monitor, "Wrapping element %s", GST_ELEMENT_NAME (element));

  element_monitor =
      GST_VALIDATE_ELEMENT_MONITOR_CAST (gst_validate_monitor_factory_create
      (GST_OBJECT_CAST (element), GST_VALIDATE_MONITOR_GET_RUNNER (monitor),
          GST_VALIDATE_MONITOR_CAST (monitor)));
  g_return_if_fail (element_monitor != NULL);

  GST_VALIDATE_MONITOR_LOCK (monitor);
  monitor->element_monitors = g_list_prepend (monitor->element_monitors,
      element_monitor);
  GST_VALIDATE_MONITOR_UNLOCK (monitor);
}

static void
_validate_bin_element_added (GstBin * bin, GstElement * element,
    GstValidateBinMonitor * monitor)
{
  g_return_if_fail (GST_VALIDATE_ELEMENT_MONITOR_GET_ELEMENT (monitor) ==
      GST_ELEMENT_CAST (bin));
  gst_validate_bin_monitor_wrap_element (monitor, element);
}
