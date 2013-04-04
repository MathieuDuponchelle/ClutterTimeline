# PiTiVi , Non-linear video editor
#
#       ui/viewer.py
#
# Copyright (c) 2005, Edward Hervey <bilboed@bilboed.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin St, Fifth Floor,
# Boston, MA 02110-1301, USA.

import platform
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import Gst
from gi.repository import GdkX11
from gi.repository import GObject
from gi.repository import GES
GdkX11  # pyflakes
from gi.repository import GstVideo
GstVideo    # pyflakes
import cairo

class ViewerWidget(Gtk.DrawingArea):
    """
    Widget for displaying properly GStreamer video sink

    @ivar settings: The settings of the application.
    @type settings: L{GlobalSettings}
    """

    __gsignals__ = {}

    def __init__(self, settings=None):
        Gtk.DrawingArea.__init__(self)
#        self.seeker = Seeker()
        self.settings = settings
        self.box = None
        self.stored = False
        self.area = None
        self.zoom = 1.0
        self.sink = None
        self.pixbuf = None
        self.pipeline = None
        self.transformation_properties = None
        # FIXME PyGi Styling with Gtk3
        #for state in range(Gtk.StateType.INSENSITIVE + 1):
            #self.modify_bg(state, self.style.black)

    def init_transformation_events(self):
        self.set_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK
                        | Gdk.EventMask.POINTER_MOTION_HINT_MASK)

    def show_box(self):
        if not self.box:
            self.box = TransformationBox(self.settings)
            self.box.init_size(self.area)
            self._update_gradient()
            self.connect("button-press-event", self.button_press_event)
            self.connect("button-release-event", self.button_release_event)
            self.connect("motion-notify-event", self.motion_notify_event)
            self.connect("size-allocate", self._sizeCb)
            self.box.set_transformation_properties(self.transformation_properties)
            self.renderbox()

    def _sizeCb(self, widget, area):
        # The transformation box is cleared when using regular rendering
        # so we need to flush the pipeline
        pass
#        self.seeker.flush()

    def hide_box(self):
        if self.box:
            self.box = None
            self.disconnect_by_func(self.button_press_event)
            self.disconnect_by_func(self.button_release_event)
            self.disconnect_by_func(self.motion_notify_event)
            #self.seeker.flush()
            self.zoom = 1.0
            if self.sink:
                self.sink.set_render_rectangle(*self.area)

    def set_transformation_properties(self, transformation_properties):
            self.transformation_properties = transformation_properties

    def _store_pixbuf(self):
        """
        When not playing, store a pixbuf of the current viewer image.
        This will allow it to be restored for the transformation box.
        """

        if self.box and self.zoom != 1.0:
            # The transformation box is active and dezoomed
            # crop away 1 pixel border to avoid artefacts on the pixbuf

            self.pixbuf = Gdk.pixbuf_get_from_window(self.get_window(),
                self.box.area.x + 1, self.box.area.y + 1,
                self.box.area.width - 2, self.box.area.height - 2)
        else:
            self.pixbuf = Gdk.pixbuf_get_from_window(self.get_window(),
                0, 0,
                self.get_window().get_width(),
                self.get_window().get_height())

        self.stored = True

    def do_realize(self):
        """
        Redefine gtk DrawingArea's do_realize method to handle multiple OSes.
        This is called when creating the widget to get the window ID.
        """
        Gtk.DrawingArea.do_realize(self)
        if platform.system() == 'Windows':
            self.window_xid = self.props.window.handle
        else:
            self.window_xid = self.get_property('window').get_xid()

    def button_release_event(self, widget, event):
        if event.button == 1:
            self.box.update_effect_properties()
            self.box.release_point()
            #self.seeker.flush()
            self.stored = False
        return True

    def button_press_event(self, widget, event):
        if event.button == 1:
            self.box.select_point(event)
        return True

    def _currentStateCb(self, pipeline, state):
        self.pipeline = pipeline
        if state == Gst.State.PAUSED:
            self._store_pixbuf()
        self.renderbox()

    def motion_notify_event(self, widget, event):
        if event.get_state() & Gdk.ModifierType.BUTTON1_MASK:
            if self.box.transform(event):
                if self.stored:
                    self.renderbox()
        return True

    def do_expose_event(self, event):
        self.area = event.area
        if self.box:
            self._update_gradient()
            if self.zoom != 1.0:
                width = int(float(self.area.width) * self.zoom)
                height = int(float(self.area.height) * self.zoom)
                area = ((self.area.width - width) / 2,
                        (self.area.height - height) / 2,
                        width, height)
                self.sink.set_render_rectangle(*area)
            else:
                area = self.area
            self.box.update_size(area)
            self.renderbox()

    def _update_gradient(self):
        self.gradient_background = cairo.LinearGradient(0, 0, 0, self.area.height)
        self.gradient_background.add_color_stop_rgb(0.00, .1, .1, .1)
        self.gradient_background.add_color_stop_rgb(0.50, .2, .2, .2)
        self.gradient_background.add_color_stop_rgb(1.00, .5, .5, .5)

    def renderbox(self):
        if self.box:
            cr = self.window.cairo_create()
            cr.push_group()

            if self.zoom != 1.0:
                # draw some nice background for zoom out
                cr.set_source(self.gradient_background)
                cr.rectangle(0, 0, self.area.width, self.area.height)
                cr.fill()

                # translate the drawing of the zoomed out box
                cr.translate(self.box.area.x, self.box.area.y)

            # clear the drawingarea with the last known clean video frame
            # translate when zoomed out
            if self.pixbuf:
                if self.box.area.width != self.pixbuf.get_width():
                    scale = float(self.box.area.width) / float(self.pixbuf.get_width())
                    cr.save()
                    cr.scale(scale, scale)
                cr.set_source_pixbuf(self.pixbuf, 0, 0)
                cr.paint()
                if self.box.area.width != self.pixbuf.get_width():
                    cr.restore()

            if self.pipeline and self.pipeline.get_state()[1] == Gst.State.PAUSED:
                self.box.draw(cr)
            cr.pop_group_to_source()
            cr.paint()
