from gi.repository import GtkClutter
GtkClutter.init([])
from gi.repository import Gst
from gi.repository import GES
from gi.repository import GObject

import sys

from gi.repository import Clutter, GObject, Gtk

from gi.repository import GLib
from gi.repository import Gdk

from viewer import ViewerWidget

from utils import Zoomable, EditingContext, Selection, SELECT, UNSELECT, Selected

from ruler import ScaleRuler

from datetime import datetime

from gettext import gettext as _

# CONSTANTS

EXPANDED_SIZE = 50
SPACING = 10

BORDER_WIDTH = 3 #For the timeline elements

ZOOM_FIT = _("Zoom Fit")

"""
Convention throughout this file:
Every GES element which name could be mistaken with a UI element
is prefixed with a little b, example : bTimeline
"""

class TimelineElement(Clutter.Actor, Zoomable):
    def __init__(self, bElement, track, timeline):
        """
        @param bElement : the backend GES.TrackElement
        @param track : the track to which the bElement belongs
        @param timeline : the containing graphic timeline.
        """
        Zoomable.__init__(self)
        Clutter.Actor.__init__(self)

        self._createBackground(track)

        self._createMarquee()

        self.bElement = bElement

        size = self.bElement.get_duration()
        self.set_size(self.nsToPixel(size), EXPANDED_SIZE)
        self.set_reactive(True)
        self._connectToEvents()
        self.timeline = timeline
        self.bElement.selected = Selected()
        self.bElement.selected.connect("selected-changed", self._selectedChangedCb)

    # Public API

    def set_size(self, width, height):
        self.background.set_size(width, height)
        self.marquee.set_size(width, height)
        self.props.width = width
        self.props.height = height

    # Internal API

    def _createBackground(self, track):
        self.background = Clutter.Rectangle()
        if track.type == GES.TrackType.AUDIO:
            self.background.set_color(Clutter.Color.new(70, 97, 118, 255))
        else:
            self.background.set_color(Clutter.Color.new(225, 232, 238, 255))
        self.background.set_border_width(BORDER_WIDTH)
        self.background.set_border_color(Clutter.Color.new(100, 100, 100, 255))
        self.add_child(self.background)

    def _createMarquee(self):
        self.marquee = Clutter.Rectangle()
        self.marquee.set_color(Clutter.Color.new(60, 60, 60, 100))
        self.add_child(self.marquee)
        self.marquee.props.visible = False

    def _connectToEvents(self):
        # Click
        # We gotta go low-level cause Clutter.ClickAction["clicked"]
        # gets emitted after Clutter.DragAction["drag-begin"]
        self.connect("button-press-event", self._clickedCb)

        # Drag and drop.
        action = Clutter.DragAction()
        self.add_action(action)
        action.connect("drag-progress", self._dragProgressCb)
        action.connect("drag-begin", self._dragBeginCb)
        action.connect("drag-end", self._dragEndCb)
        self.dragAction = action

    # Interface (Zoomable)

    def zoomChanged(self):
        size = self.bElement.get_duration()
        self.set_size(self.nsToPixel(size), EXPANDED_SIZE)        

    # Callbacks

    def _clickedCb(self, action, actor):
        #TODO : Let's be more specific, masks etc ..
        self.timeline.selection.setToObj(self.bElement, SELECT)

    def _dragBeginCb(self, action, actor, event_x, event_y, modifiers):
        self._context = EditingContext(self.bElement, self.timeline.bTimeline, GES.EditMode.EDIT_NORMAL, GES.Edge.EDGE_NONE, self.timeline.selection.getSelectedTrackElements(), None)

        self._dragBeginStart = self.bElement.get_start()
        self.dragBeginStartX = event_x

    def _dragProgressCb(self, action, actor, delta_x, delta_y):
        # We can't use delta_x here because it fluctuates weirdly.
        delta_x = self.dragAction.get_motion_coords()[0] - self.dragBeginStartX
        new_start = self._dragBeginStart + self.pixelToNs(delta_x)
        self._context.editTo(new_start, 0)
        return False

    def _dragEndCb(self, action, actor, event_x, event_y, modifiers):
        self._context.finish()

    def _selectedChangedCb(self, selected, isSelected):
        self.marquee.props.visible = isSelected

class Timeline(Clutter.ScrollActor, Zoomable):
    def __init__(self):
        Clutter.ScrollActor.__init__(self)
        Zoomable.__init__(self)
        self.set_background_color(Clutter.Color.new(31, 30, 33, 255))
        self.props.width = 1920
        self.props.height = 500
        self.elements = []
        self.selection = Selection()

    # Public API

    def setBackendTimeline(self, bTimeline):
        """
        @param bTimeline : the backend GES.Timeline which we interface.
        Does all the necessary connections.
        """
        self.bTimeline = bTimeline
        for track in bTimeline.get_tracks():
            track.connect("track-element-added", self._trackElementAddedCb)
            track.connect("track-element-removed", self._trackElementRemovedCb)
        self.bTimeline.connect("layer-added", self._layerAddedCb)
        self.bTimeline.connect("layer-removed", self._layerRemovedCb)
        self.zoomChanged()

    #Stage was clicked with nothing under the pointer
    def emptySelection(self):
        """
        Empty the current selection.
        """
        self.selection.setSelection(self.selection.getSelectedTrackElements(), UNSELECT)

    #Internal API

    def _addTimelineElement(self, track, bElement):
        element = TimelineElement(bElement, track, self)

        bElement.connect("notify::start", self._elementStartChangedCb, element)
        bElement.connect("notify::duration", self._elementDurationChangedCb, element)
        bElement.connect("notify::in-point", self._elementInPointChangedCb, element)

        self.elements.append(element)

        self._setElementY(element)

        self.add_child(element)

        self._setElementX(element)

    def _removeTimelineElement(self, track, bElement):
        bElement.disconnect_by_func("notify::start", self._elementStartChangedCb)
        bElement.disconnect_by_func("notify::duration", self._elementDurationChangedCb)
        bElement.disconnect_by_func("notify::in-point", self._elementInPointChangedCb)

    def _setElementX(self, element):
        element.props.x = self.nsToPixel(element.bElement.get_start())

    # Crack, change that when we have retractable layers
    def _setElementY(self, element):
        y = 0
        bElement = element.bElement
        track_type = bElement.get_track_type()

        if (track_type == GES.TrackType.AUDIO):
            y = len(self.bTimeline.get_layers()) * (EXPANDED_SIZE + SPACING)

        y += bElement.get_parent().get_layer().get_priority() * (EXPANDED_SIZE + SPACING) + SPACING

        element.props.y = y

    # Interface overrides (Zoomable)

    def zoomChanged(self):
        self.props.width = self.nsToPixel(self.bTimeline.get_duration())
        for element in self.elements:
            self._setElementX(element)

    # Callbacks

    def _layerAddedCb(self, timeline, layer):
        for element in self.elements:
            self._setElementY(element)
        layer.connect("clip-added", self._clipAddedCb)
        layer.connect("clip-removed", self._clipRemovedCb)

    def _layerRemovedCb(self, timeline, layer):
        layer.disconnect_by_func(self._clipAddedCb)
        layer.disconnect_by_func(self._clipRemovedCb)

    def _clipAddedCb(self, layer, clip):
        clip.connect("child-added", self._elementAddedCb)
        clip.connect("child-removed", self._elementRemovedCb)

    def _clipRemovedCb(self, layer, clip):
        clip.disconnect_by_func(self._elementAddedCb)
        clip.disconnect_by_func(self._elementRemovedCb)

    def _elementAddedCb(self, clip, bElement):
        pass

    def _elementRemovedCb(self):
        pass

    def _trackElementAddedCb(self, track, bElement):
        self._addTimelineElement(track, bElement)

    def _trackElementRemovedCb(self, track, bElement):
        self._removeTimelineElement(track, bElement)

    def _elementStartChangedCb(self, bElement, start, element):
        self._setElementX(element)

    def _elementDurationChangedCb(self, bElement, duration, element):
        pass

    def _elementInPointChangedCb(self, bElement, inpoint, element):
        pass

def quit_(stage):
    Gtk.main_quit()

def quit2_(*args, **kwargs):
    Gtk.main_quit()

class ZoomBox(Gtk.HBox, Zoomable):
    def __init__(self):
        """
        This will hold the widgets responsible for zooming.
        """
        Gtk.HBox.__init__(self)
        Zoomable.__init__(self)

        zoom_fit_btn = Gtk.Button()
        zoom_fit_btn.set_relief(Gtk.ReliefStyle.NONE)
        zoom_fit_btn.set_tooltip_text(ZOOM_FIT)
        zoom_fit_icon = Gtk.Image()
        zoom_fit_icon.set_from_stock(Gtk.STOCK_ZOOM_FIT, Gtk.IconSize.BUTTON)
        zoom_fit_btn_hbox = Gtk.HBox()
        zoom_fit_btn_hbox.pack_start(zoom_fit_icon, False, True, 0)
        zoom_fit_btn_hbox.pack_start(Gtk.Label(_("Zoom")), False, True, 0)
        zoom_fit_btn.add(zoom_fit_btn_hbox)
        zoom_fit_btn.connect("clicked", self._zoomFitCb)
        self.pack_start(zoom_fit_btn, False, True, 0)

        # zooming slider
        self._zoomAdjustment = Gtk.Adjustment()
        self._zoomAdjustment.set_value(Zoomable.getCurrentZoomLevel())
        self._zoomAdjustment.connect("value-changed", self._zoomAdjustmentChangedCb)
        self._zoomAdjustment.props.lower = 0
        self._zoomAdjustment.props.upper = Zoomable.zoom_steps
        zoomslider = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, adjustment=self._zoomAdjustment)
        zoomslider.props.draw_value = False
        zoomslider.set_tooltip_text(_("Zoom Timeline"))
        zoomslider.connect("scroll-event", self._zoomSliderScrollCb)
        zoomslider.set_size_request(100, 0)  # At least 100px wide for precision
        self.pack_start(zoomslider, True, True, 0)

        self.show_all()

        self._updateZoomSlider = True

    def _zoomAdjustmentChangedCb(self, adjustment):
        # GTK crack
        self._updateZoomSlider = False
        Zoomable.setZoomLevel(int(adjustment.get_value()))
        self.zoomed_fitted = False
        self._updateZoomSlider = True

    def _zoomFitCb(self, button):
        pass

    def _zoomSliderScrollCb(self, unused, event):
        value = self._zoomAdjustment.get_value()
        if event.direction in [Gdk.ScrollDirection.UP, Gdk.ScrollDirection.RIGHT]:
            self._zoomAdjustment.set_value(value + 1)
        elif event.direction in [Gdk.ScrollDirection.DOWN, Gdk.ScrollDirection.LEFT]:
            self._zoomAdjustment.set_value(value - 1)

    def zoomChanged(self):
        if self._updateZoomSlider:
            self._zoomAdjustment.set_value(self.getCurrentZoomLevel())

class TimelineTest(Zoomable):
    def __init__(self):
        Zoomable.__init__(self)
        GObject.threads_init()
        self.window = Gtk.Window()
        self.embed = GtkClutter.Embed()
        self.embed.show()
        vbox = Gtk.VBox()
        self.window.add(vbox)

        self.zoomBox = ZoomBox()
        vbox.pack_end(self.zoomBox, False, False, False)

        self._packScrollbars(vbox)

        self.viewer = ViewerWidget()

        vbox.pack_end(self.viewer, False, False, False)

        self.viewer.set_size_request(200, 200)

        stage = self.embed.get_stage()
        stage.set_background_color(Clutter.Color.new(31, 30, 33, 255))

        self.stage = stage

        self.stage.set_throttle_motion_events(True)

        self.window.maximize()

        stage.show()

        widget = Timeline()

        stage.add_child(widget)
        stage.connect("destroy", quit_)
        stage.connect("button-press-event", self._clickedCb)
        self.timeline = widget

        self.window.show_all()

    def _packScrollbars(self, vbox):
        self.hadj = Gtk.Adjustment()
        self.hadj.connect("value-changed", self._updateScrollPosition)

        self._hscrollBar = Gtk.HScrollbar(self.hadj)
        vbox.pack_end(self._hscrollBar, False, True, False)

        self.ruler = ScaleRuler(self, self.hadj)

        self.ruler.set_size_request(0, 25)

        vbox.pack_end(self.embed, True, True, True)
        vbox.pack_end(self.ruler, False, True, False)
        

    def _updateScrollPosition(self, adjustment):
        self._scroll_pos_ns = Zoomable.pixelToNs(self.hadj.get_value())
        point = Clutter.Point()
        point.x = self.hadj.get_value()
        point.y = 0
        self.timeline.scroll_to_point(point)

    def zoomChanged(self):
        self.updateHScrollAdjustments()

    def updateHScrollAdjustments(self):
        """
        Recalculate the horizontal scrollbar depending on the timeline duration.
        """
        timeline_ui_width = self.embed.get_allocation().width
#        controls_width = self.controls.get_allocation().width
#        scrollbar_width = self._vscrollbar.get_allocation().width
        controls_width = 0
        scrollbar_width = 0
        contents_size = Zoomable.nsToPixel(self.bTimeline.props.duration)

        widgets_width = controls_width + scrollbar_width
        end_padding = 250  # Provide some space for clip insertion at the end

        self.hadj.props.lower = 0
        self.hadj.props.upper = contents_size + widgets_width + end_padding
        self.hadj.props.page_size = timeline_ui_width
        self.hadj.props.page_increment = contents_size * 0.9
        self.hadj.props.step_increment = contents_size * 0.1

        if contents_size + widgets_width <= timeline_ui_width:
            # We're zoomed out completely, re-enable automatic zoom fitting
            # when adding new clips.
            #self.log("Setting 'zoomed_fitted' to True")
            self.zoomed_fitted = True


    def run(self):
        self.testTimeline(self.timeline)
        GLib.io_add_watch(sys.stdin, GLib.IO_IN, quit2_)
        Gtk.main()

    def goToPoint(self, timeline):
        point = Clutter.Point()
        point.x = 1000
        point.y = 0
        timeline.scroll_to_point(point)
        return False

    def addClipToLayer(self, layer, asset, start, duration, inpoint):
        clip = asset.extract()
        clip.set_start(start * Gst.SECOND)
        clip.set_duration(duration * Gst.SECOND)
        clip.set_inpoint(inpoint * Gst.SECOND)
        layer.add_clip(clip)

    def handle_message(self, bus, message):
        if message.type == Gst.MessageType.ELEMENT:
            if message.has_name('prepare-window-handle'):
                Gdk.threads_enter()
                self.sink = message.src
                self.sink.set_window_handle(self.viewer.window_xid)
                self.sink.expose()
                Gdk.threads_leave()
            elif message.type == Gst.MessageType.STATE_CHANGED:
                prev, new, pending = message.parse_state_changed()
        return True

    def _clickedCb(self, stage, event):
        actor = self.stage.get_actor_at_pos(Clutter.PickMode.REACTIVE, event.x, event.y)
        if actor == stage:
            self.timeline.emptySelection()

    def _doAssetAddedCb(self, project, asset, layer):
        self.addClipToLayer(layer, asset, 2, 25, 5)

        self.pipeline = GES.TimelinePipeline()
        self.pipeline.add_timeline(layer.get_timeline())

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.handle_message)
        self.pipeline.set_state(Gst.State.PLAYING)
        Zoomable.setZoomLevel(50)

    def testTimeline(self, timeline):
        timeline.set_easing_duration(600)

        Gst.init([])
        GES.init()

        self.project = GES.Project(uri=None, extractable_type=GES.Timeline)

        bTimeline = GES.Timeline()
        bTimeline.add_track(GES.Track.audio_raw_new())
        bTimeline.add_track(GES.Track.video_raw_new())

        timeline.setBackendTimeline(bTimeline)

        layer = GES.TimelineLayer()
        bTimeline.add_layer(layer)

        self.bTimeline = bTimeline

        self.project.connect("asset-added", self._doAssetAddedCb, layer)
        self.project.create_asset("file://" + sys.argv[1], GES.UriClip)

if __name__ == "__main__":
    # Basic argument handling, no need for getopt here
    if len(sys.argv) < 2:
        print "Supply a uri as argument"
        sys.exit()

    print "Starting stupid demo, using uri as a new clip, with start = 2, duration = 25 and inpoint = 5."
    print "Use ipython if you want to interact with the timeline in a more interesting way"
    print "ipython ; %gui gtk3 ; %run timeline.py ; help yourself"

    widget = TimelineTest()
    widget.run()