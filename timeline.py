from gi.repository import GtkClutter
GtkClutter.init([])
from gi.repository import Gst
from gi.repository import GES
from gi.repository import GObject

import collections
import hashlib
import os
import sqlite3
import sys
import xdg.BaseDirectory as xdg_dirs

from gi.repository import Clutter, GObject, Gtk, Cogl

from gi.repository import GLib
from gi.repository import Gdk
from gi.repository import GdkPixbuf

from viewer import ViewerWidget

from utils import Zoomable, EditingContext, Selection, SELECT, UNSELECT, Selected

from ruler import ScaleRuler

from datetime import datetime

from gettext import gettext as _

from pipeline import Pipeline

from layer import VideoLayerControl, AudioLayerControl

# CONSTANTS

EXPANDED_SIZE = 65
SPACING = 10
CONTROL_WIDTH = 250

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

        self.timeline = timeline

        self.bElement = bElement

        self._createBackground(track)

        self._createPreview()

        self._createMarquee()

        self._createGhostclip()

        self.track_type = self.bElement.get_track_type() # This won't change

        size = self.bElement.get_duration()
        self.set_size(self.nsToPixel(size), EXPANDED_SIZE)
        self.set_reactive(True)
        self._connectToEvents()
        self.bElement.selected = Selected()
        self.bElement.selected.connect("selected-changed", self._selectedChangedCb)

    # Public API

    def set_size(self, width, height):
        self.save_easing_state()
        self.set_easing_duration(600)
        self.marquee.set_size(width, height)
        self.preview.set_size(width, height)
        self.props.width = width
        self.props.height = height
        self.restore_easing_state()

    def updateGhostclip(self, priority, y, isControlledByBrother):
        # Only tricky part of the code, can be called by the linked track element.
        if priority < 0:
            return

        # Here we make it so the calculation is the same for audio and video.
        if self.track_type == GES.TrackType.AUDIO and not isControlledByBrother:
            y -= self.nbrLayers * (EXPANDED_SIZE + SPACING)

        # And here we take into account the fact that the pointer might actually be
        # on the other track element, meaning we have to offset it.
        if isControlledByBrother:
            if self.track_type == GES.TrackType.AUDIO:
                y += self.nbrLayers * (EXPANDED_SIZE + SPACING)
            else:
                y -= self.nbrLayers * (EXPANDED_SIZE + SPACING)

        # Would that be a new layer ?
        if priority == self.nbrLayers:
            self.ghostclip.set_size(self.props.width, SPACING)
            self.ghostclip.props.y = priority * (EXPANDED_SIZE + SPACING)
            if self.track_type == GES.TrackType.AUDIO:
                self.ghostclip.props.y += self.nbrLayers * (EXPANDED_SIZE + SPACING)
            self.ghostclip.props.visible = True
        else:
            # No need to mockup on the same layer
            if priority == self.bElement.get_parent().get_layer().get_priority():
                self.ghostclip.props.visible = False
            # We would be moving to an existing layer.
            elif priority < self.nbrLayers:
                self.ghostclip.set_size(self.props.width, EXPANDED_SIZE)
                self.ghostclip.props.y = priority * (EXPANDED_SIZE + SPACING) + SPACING
                if self.track_type == GES.TrackType.AUDIO:
                    self.ghostclip.props.y += self.nbrLayers * (EXPANDED_SIZE + SPACING)
                self.ghostclip.props.visible = True

    # Internal API

    def _createGhostclip(self):
        self.ghostclip = Clutter.Actor.new()
        # TODO: border is missing. Add it or leave it out?
        self.ghostclip.set_background_color(Clutter.Color.new(100, 100, 100, 50))
        self.ghostclip.props.visible = False
        self.timeline.add_child(self.ghostclip)

    def _createBackground(self, track):
        # TODO: border is missing. Add it or leave it out?
        # TODO: which style to use? props or setter?
        if track.type == GES.TrackType.AUDIO:
            self.props.background_color = Clutter.Color.new(70, 79, 118, 255)
        else:
            self.set_background_color(Clutter.Color.new(225, 232, 238, 255))

    def _createPreview(self):
        self.preview = get_preview_for_object(self.bElement)
        self.add_child(self.preview)

    def _createMarquee(self):
        # TODO: difference between Actor.new() and Actor()?
        self.marquee = Clutter.Actor()
        self.marquee.set_background_color(Clutter.Color.new(60, 60, 60, 100))
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

    def _getLayerForY(self, y):
        if self.bElement.get_track_type() == GES.TrackType.AUDIO:
            y -= self.nbrLayers * (EXPANDED_SIZE + SPACING)
        priority = int(y / (EXPANDED_SIZE + SPACING))
        return priority

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

        # This can't change during a drag, so we can safely compute it now for drag events.
        self.nbrLayers = len(self.timeline.bTimeline.get_layers())
        # We can also safely find if the object has a brother element
        self.brother = self.timeline.findBrother(self.bElement)
        if self.brother:
            self.brother.nbrLayers = self.nbrLayers

        self._dragBeginStart = self.bElement.get_start()
        self.dragBeginStartX = event_x
        self.dragBeginStartY = event_y

    def _dragProgressCb(self, action, actor, delta_x, delta_y):
        # We can't use delta_x here because it fluctuates weirdly.
        coords = self.dragAction.get_motion_coords()
        delta_x = coords[0] - self.dragBeginStartX
        delta_y = coords[1] - self.dragBeginStartY

        y = coords[1] + self.timeline._container.point.y

        priority = self._getLayerForY(y)

        self.ghostclip.props.x = self.nsToPixel(self._dragBeginStart) + delta_x
        self.updateGhostclip(priority, y, False)
        if self.brother:
            self.brother.ghostclip.props.x = self.nsToPixel(self._dragBeginStart) + delta_x
            self.brother.updateGhostclip(priority, y, True)

        new_start = self._dragBeginStart + self.pixelToNs(delta_x)

        if not self.ghostclip.props.visible:
            self._context.editTo(new_start, self.bElement.get_parent().get_layer().get_priority())
        else:
            self._context.editTo(self._dragBeginStart, self.bElement.get_parent().get_layer().get_priority())
        return False

    def _dragEndCb(self, action, actor, event_x, event_y, modifiers):
        coords = self.dragAction.get_motion_coords()
        delta_x = coords[0] - self.dragBeginStartX
        new_start = self._dragBeginStart + self.pixelToNs(delta_x)

        priority = self._getLayerForY(coords[1] + self.timeline._container.point.y)
        priority = min(priority, len(self.timeline.bTimeline.get_layers()))

        self.ghostclip.props.visible = False
        if self.brother:
            self.brother.ghostclip.props.visible = False

        priority = max(0, priority)
        self._context.editTo(new_start, priority)
        self._context.finish()

    def _selectedChangedCb(self, selected, isSelected):
        self.marquee.props.visible = isSelected

class Timeline(Clutter.ScrollActor, Zoomable):
    def __init__(self, container):
        Clutter.ScrollActor.__init__(self)
        Zoomable.__init__(self)
        self.set_background_color(Clutter.Color.new(31, 30, 33, 255))
        self.props.width = 1920
        self.props.height = 500
        self.elements = []
        self.selection = Selection()
        self._createPlayhead()
        self._container = container

    # Public API

    def setPipeline(self, pipeline):
        pipeline.connect('position', self._positionCb)

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

    def findBrother(self, element):
        father = element.get_parent()
        for elem in self.elements:
            if elem.bElement.get_parent() == father and elem.bElement != element:
                return elem
        return None

    #Internal API

    def _positionCb(self, pipeline, position):
        self.playhead.set_z_position(1)
        self.playhead.props.x = self.nsToPixel(position)

    def _updatePlayHead(self):
        height = len(self.bTimeline.get_layers()) * (EXPANDED_SIZE + SPACING) * 2
        self.playhead.set_size(2, height)

    def _createPlayhead(self):
        self.playhead = Clutter.Actor()
        self.playhead.set_background_color(Clutter.Color.new(200, 0, 0, 255))
        self.playhead.set_size(0, 0)
        self.playhead.set_position(0, 0)
        self.add_child(self.playhead)

    def _addTimelineElement(self, track, bElement):
        element = TimelineElement(bElement, track, self)
        element.set_z_position(-1)

        bElement.connect("notify::start", self._elementStartChangedCb, element)
        bElement.connect("notify::duration", self._elementDurationChangedCb, element)
        bElement.connect("notify::in-point", self._elementInPointChangedCb, element)
        bElement.connect("notify::priority", self._elementPriorityChangedCb, element)

        self.elements.append(element)

        self._setElementY(element)

        self.add_child(element)

        self._setElementX(element)

    def _removeTimelineElement(self, track, bElement):
        bElement.disconnect_by_func("notify::start", self._elementStartChangedCb)
        bElement.disconnect_by_func("notify::duration", self._elementDurationChangedCb)
        bElement.disconnect_by_func("notify::in-point", self._elementInPointChangedCb)

    def _setElementX(self, element):
        element.save_easing_state()
        element.set_easing_duration(600)
        element.props.x = self.nsToPixel(element.bElement.get_start())
        element.restore_easing_state()

    # Crack, change that when we have retractable layers
    def _setElementY(self, element):
        element.save_easing_state()
        y = 0
        bElement = element.bElement
        track_type = bElement.get_track_type()

        if (track_type == GES.TrackType.AUDIO):
            y = len(self.bTimeline.get_layers()) * (EXPANDED_SIZE + SPACING)

        y += bElement.get_parent().get_layer().get_priority() * (EXPANDED_SIZE + SPACING) + SPACING

        element.props.y = y
        element.restore_easing_state()

    def _redraw(self):
        self.save_easing_state()
#        self.set_easing_duration(0)
        self.props.width = self.nsToPixel(self.bTimeline.get_duration()) + 250
        for element in self.elements:
            self._setElementX(element)
        self.restore_easing_state()


    # Interface overrides (Zoomable)

    def zoomChanged(self):
        self._redraw()

    # Callbacks

    def _layerAddedCb(self, timeline, layer):
        self.save_easing_state()
        self.props.height = (len(self.bTimeline.get_layers()) + 1) * (EXPANDED_SIZE + SPACING) * 2 + SPACING
        self.restore_easing_state()
        self._container.vadj.props.upper = self.props.height
        self._container.addLayerControl(layer)
        self._updatePlayHead()

    def _layerRemovedCb(self, timeline, layer):
        layer.disconnect_by_func(self._clipAddedCb)
        layer.disconnect_by_func(self._clipRemovedCb)
        self._updatePlayHead()

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

    def _elementPriorityChangedCb(self, bElement, priority, element):
        self._setElementY(element)

    def _elementStartChangedCb(self, bElement, start, element):
        self._setElementX(element)

    def _elementDurationChangedCb(self, bElement, duration, element):
        pass

    def _elementInPointChangedCb(self, bElement, inpoint, element):
        pass

    def _layerPriorityChangedCb(self, layer, priority):
        self._redraw()

def quit_(stage):
    Gtk.main_quit()

def quit2_(*args, **kwargs):
    Gtk.main_quit()

class ZoomBox(Gtk.HBox, Zoomable):
    def __init__(self, timeline):
        """
        This will hold the widgets responsible for zooming.
        """
        Gtk.HBox.__init__(self)
        Zoomable.__init__(self)

        self.timeline = timeline

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
        self.timeline.zoomFit()

    def _zoomSliderScrollCb(self, unused, event):
        value = self._zoomAdjustment.get_value()
        if event.direction in [Gdk.ScrollDirection.UP, Gdk.ScrollDirection.RIGHT]:
            self._zoomAdjustment.set_value(value + 1)
        elif event.direction in [Gdk.ScrollDirection.DOWN, Gdk.ScrollDirection.LEFT]:
            self._zoomAdjustment.set_value(value - 1)

    def zoomChanged(self):
        if self._updateZoomSlider:
            self._zoomAdjustment.set_value(self.getCurrentZoomLevel())

class ControlActor(GtkClutter.Actor):
    def __init__(self, container, widget, layer):
        GtkClutter.Actor.__init__(self)
        self.get_widget().add(widget)
        self.set_reactive(True)
        self.layer = layer
        self._setUpDragAndDrop()
        self._container = container

    def _getLayerForY(self, y):
        if self.isAudio:
            y -= self.nbrLayers * (EXPANDED_SIZE + SPACING)
        priority = int(y / (EXPANDED_SIZE + SPACING))
        return priority

    def _setUpDragAndDrop(self):
        self.dragAction = Clutter.DragAction()
        self.add_action(self.dragAction)
        self.dragAction.connect("drag-begin", self._dragBeginCb)
        self.dragAction.connect("drag-progress", self._dragProgressCb)
        self.dragAction.connect("drag-end", self._dragEndCb)

    def _dragBeginCb(self, action, actor, event_x, event_y, modifiers):
        self.nbrLayers = len(self._container.timeline.bTimeline.get_layers())
        self._dragBeginStartX = event_x

    def _dragProgressCb(self, action, actor, delta_x, delta_y):
        y = self.dragAction.get_motion_coords()[1]
        priority = self._getLayerForY(y)
        if self.layer.get_priority() != priority:
            self._container.highlightSeparator(priority)
        else:
            self._container.highlightSeparator(1000)
        return False

    def _dragEndCb(self, action, actor, event_x, event_y, modifiers):
        priority = self._getLayerForY(event_y)
        if self.layer.get_priority() != priority and priority >= 0 and priority < self.nbrLayers:
            self._container.moveLayer(self, priority)

class TimelineTest(Zoomable):
    def __init__(self):
        gtksettings = Gtk.Settings.get_default()
        gtksettings.set_property("gtk-application-prefer-dark-theme", True)
        Zoomable.__init__(self)
        GObject.threads_init()
        self.window = Gtk.Window()
        self.embed = GtkClutter.Embed()
        self.embed.show()
        vbox = Gtk.VBox()
        self.window.add(vbox)

        self.controlActors = []
        self.trackControls = []

        self.point = Clutter.Point()
        self.point.x = 0
        self.point.y = 0

        self.zoomBox = ZoomBox(self)
        vbox.pack_end(self.zoomBox, False, False, False)

        self._packScrollbars(vbox)

        self.viewer = ViewerWidget()

        vbox.pack_end(self.viewer, False, False, False)

        self.viewer.set_size_request(200, 200)

        stage = self.embed.get_stage()
        stage.set_background_color(Clutter.Color.new(31, 30, 33, 255))

        self.stage = stage

        self.embed.connect("scroll-event", self._scrollEventCb)

        self.stage.set_throttle_motion_events(True)

        self.window.maximize()

        stage.show()

        widget = Timeline(self)

        stage.add_child(widget)
        widget.set_position(CONTROL_WIDTH, 0)
        stage.connect("destroy", quit_)
        stage.connect("button-press-event", self._clickedCb)
        self.timeline = widget

        self.scrolled = 0
        self.window.show_all()
        self.ruler.hide()

    def _setTrackControlPosition(self, control):
        y = control.layer.get_priority() * (EXPANDED_SIZE + SPACING) + SPACING
        if control.isAudio:
            y += len(self.timeline.bTimeline.get_layers()) * (EXPANDED_SIZE + SPACING)
        control.set_position(0, y)

    def _reorderLayerActors(self):
        for control in self.controlActors: 
            control.save_easing_state()
            control.set_easing_mode(Clutter.AnimationMode.EASE_OUT_BACK)
            self._setTrackControlPosition(control)
            control.restore_easing_state()

    def moveLayer(self, control, target):
        movedLayer = control.layer
        priority = movedLayer.get_priority()
        self.timeline.bTimeline.enable_update(False)
        movedLayer.props.priority = 999

        if priority > target:
            for layer in self.timeline.bTimeline.get_layers():
                prio = layer.get_priority()
                if target <= prio < priority:
                    layer.props.priority = prio + 1
        elif priority < target:
            for layer in self.timeline.bTimeline.get_layers():
                prio = layer.get_priority()
                if priority < prio <= target:
                    layer.props.priority = prio - 1
        movedLayer.props.priority = target
        self.highlightSeparator(1000)

        self._reorderLayerActors()
        self.timeline.bTimeline.enable_update(True)

    def addTrackControl(self, layer, isAudio):
        if isAudio:
            control = AudioLayerControl(self, layer)
        else:
            control = VideoLayerControl(self, layer)

        controlActor = ControlActor(self, control, layer)
        controlActor.isAudio = isAudio
        controlActor.layer = layer
        controlActor.set_size(CONTROL_WIDTH, EXPANDED_SIZE + SPACING)

        self.stage.add_child(controlActor)
        self.trackControls.append(control)
        self.controlActors.append(controlActor)

    def highlightSeparator(self, priority):
        for control in self.trackControls:
            if control.layer.get_priority() == priority:
                control.setSeparatorHighlight(True)
            else:
                control.setSeparatorHighlight(False)

    def selectLayerControl(self, layer_control):
        for control in self.trackControls:
            control.selected = False
        layer_control.selected = True

    def addLayerControl(self, layer):
        self.addTrackControl(layer, False)
        self.addTrackControl(layer, True)
        self._reorderLayerActors()

    def _packScrollbars(self, vbox):
        self.hadj = Gtk.Adjustment()
        self.vadj = Gtk.Adjustment()
        self.hadj.connect("value-changed", self._updateScrollPosition)
        self.vadj.connect("value-changed", self._updateScrollPosition)

        self._vscrollbar = Gtk.VScrollbar(self.vadj)

        def scrollbar_show_cb(scrollbar):
            scrollbar.hide()

#        self._vscrollbar.connect("show", scrollbar_show_cb)

        self._hscrollBar = Gtk.HScrollbar(self.hadj)
        vbox.pack_end(self._hscrollBar, False, True, False)

        self.ruler = ScaleRuler(self, self.hadj)
        self.ruler.setProjectFrameRate(24.)

        self.ruler.set_size_request(0, 25)
        self.ruler.hide()

        self.vadj.props.lower = 0
        self.vadj.props.upper = 500
        self.vadj.props.page_size = 250

        hbox = Gtk.HBox()
        hbox.set_size_request(-1, 500)
        hbox.pack_start(self.embed, True, True, True)
        hbox.pack_start(self._vscrollbar, False, True, False)

        vbox.pack_end(hbox, True, True, True)

        hbox = Gtk.HBox()
        label = Gtk.Label("layer controls")
        label.set_size_request(CONTROL_WIDTH, -1)
        hbox.pack_start(label, False, True, False)
        hbox.pack_start(self.ruler, True, True, True)

        vbox.pack_end(hbox, False, True, False)

    def _updateScrollPosition(self, adjustment):
        self._scroll_pos_ns = Zoomable.pixelToNs(self.hadj.get_value())
        point = Clutter.Point()
        point.x = self.hadj.get_value()
        point.y = self.vadj.get_value()
        self.point = point
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

    def _setBestZoomRatio(self):
        """
        Set the zoom level so that the entire timeline is in view.
        """
        ruler_width = self.ruler.get_allocation().width
        # Add Gst.SECOND - 1 to the timeline duration to make sure the
        # last second of the timeline will be in view.
        duration = self.timeline.bTimeline.get_duration()
        if duration == 0:
            self.debug("The timeline duration is 0, impossible to calculate zoom")
            return

        timeline_duration = duration + Gst.SECOND - 1
        timeline_duration_s = int(timeline_duration / Gst.SECOND)

        #self.debug("duration: %s, timeline duration: %s" % (print_ns(duration),
    #       print_ns(timeline_duration)))

        ideal_zoom_ratio = float(ruler_width) / timeline_duration_s
        nearest_zoom_level = Zoomable.computeZoomLevel(ideal_zoom_ratio)
        #self.debug("Ideal zoom: %s, nearest_zoom_level %s", ideal_zoom_ratio, nearest_zoom_level)
        Zoomable.setZoomLevel(nearest_zoom_level)
        #self.timeline.props.snapping_distance = \
        #    Zoomable.pixelToNs(self.app.settings.edgeSnapDeadband)

        # Only do this at the very end, after updating the other widgets.
        #self.log("Setting 'zoomed_fitted' to True")
        self.zoomed_fitted = True


    def zoomFit(self):
        self._hscrollBar.set_value(0)
        self._setBestZoomRatio()

    def _scrollLeft(self):
        self._hscrollBar.set_value(self._hscrollBar.get_value() -
            self.hadj.props.page_size ** (2.0 / 3.0))

    def _scrollRight(self):
        self._hscrollBar.set_value(self._hscrollBar.get_value() +
            self.hadj.props.page_size ** (2.0 / 3.0))

    def _scrollUp(self):
        self._vscrollbar.set_value(self._vscrollbar.get_value() -
            self.vadj.props.page_size ** (2.0 / 3.0))

    def _scrollDown(self):
        self._vscrollbar.set_value(self._vscrollbar.get_value() +
            self.vadj.props.page_size ** (2.0 / 3.0))

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

    def doSeek(self):
        #self.pipeline.simple_seek(3000000000)
        return False

    def _doAssetAddedCb(self, project, asset, layer):
        self.addClipToLayer(layer, asset, 2, 10, 5)
        self.addClipToLayer(layer, asset, 15, 10, 5)

        self.pipeline = Pipeline()
        self.pipeline.add_timeline(layer.get_timeline())

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.handle_message)
        #self.pipeline.togglePlayback()
        self.pipeline.activatePositionListener()
        self.timeline.setPipeline(self.pipeline)
        GObject.timeout_add(1000, self.doSeek)
        Zoomable.setZoomLevel(50)

    def _scrollEventCb(self, embed, event):
        # FIXME : see https://bugzilla.gnome.org/show_bug.cgi?id=697522
        deltas = event.get_scroll_deltas()
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            if deltas[2] < 0:
                Zoomable.zoomIn()
            elif deltas[2] > 0:
                Zoomable.zoomOut()
        elif event.state & Gdk.ModifierType.SHIFT_MASK:
            if deltas[2] > 0:
                self._scrollDown()
            elif deltas[2] < 0:
                self._scrollUp()
        else:
            if deltas[2] > 0:
                self._scrollRight()
            elif deltas[2] < 0:
                self._scrollLeft()
        self.scrolled += 1

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

def get_preview_for_object(bElement):
    track_type = bElement.get_track_type()
    if track_type == GES.TrackType.AUDIO:
        # FIXME: RandomAccessAudioPreviewer doesn't work yet
        # previewers[key] = RandomAccessAudioPreviewer(instance, uri)
        # TODO: return waveform previewer
        return Clutter.Actor()
    elif track_type == GES.TrackType.VIDEO:
        if bElement.get_parent().is_image():
            # TODO: return still image previewer
            return Clutter.Actor()
        else:
            return VideoPreviewer(bElement)
    else:
        return Clutter.Actor()

class VideoPreviewer(Clutter.ScrollActor, Zoomable):
    def __init__(self, bElement):
        """
        @param bElement : the backend GES.TrackElement
        @param track : the track to which the bElement belongs
        @param timeline : the containing graphic timeline.
        """
        Zoomable.__init__(self)
        Clutter.ScrollActor.__init__(self)

        self.set_scroll_mode(Clutter.ScrollMode.HORIZONTALLY)

        self.uri = bElement.props.uri

        self.bElement = bElement

        # TODO: do these connections work?
        self.bElement.connect("notify::duration", self.element_changed)
        self.bElement.connect("notify::in-point", self.element_changed)

        self.duration = self.bElement.get_duration()
        self.in_point = self.bElement.get_inpoint()

        self.thumb_margin = 5
        self.thumb_height = EXPANDED_SIZE - 2 * self.thumb_margin
        # self.thumb_width will be set by self._setupPipeline()

        # TODO: read this property from the settings
        self.thumb_period = 1 * 10**9 # 1 second in nanoseconds

        # maps (quantized) times to Thumbnail objects
        self.thumbs = {}

        self.thumb_cache = ThumbnailCache(uri=self.uri)

        self.queue = []

        self.waiting_timestamp = None

        self._setupPipeline();

        self.callback_id = None

    # Internal API

    def _setupPipeline(self):
        """
        Create the pipeline.

        It has the form "playbin ! thumbnailsink" where thumbnailsink
        is a Bin made out of "capsfilter ! gdkpixbufsink"
        """
        self.pipeline = Gst.ElementFactory.make("playbin", None)
        self.pipeline.props.uri = self.uri
        self.pipeline.props.flags = 1 # Only render video
        self.pipeline.get_bus().add_signal_watch()
        self.pipeline.get_bus().connect("message", self.bus_message_handler)

        # Use a capsfilter to scale the video to the desired size
        # (fixed height and pixel aspect ratio, variable width)
        type = "video/x-raw, height=(int)%d, pixel-aspect-ratio=(fraction)1/1" % self.thumb_height
        caps = Gst.caps_from_string(type)
        capsfilter = Gst.ElementFactory.make("capsfilter", "thumbnailcapsfilter")
        capsfilter.props.caps = caps

        # Create the gdkpixbufsink
        self.gdkpixbufsink = Gst.ElementFactory.make("gdkpixbufsink", None)

        # Set up the thumbnailsink and add a sink pad
        thumbnailsink = Gst.Bin(name="thumbnailsink")
        thumbnailsink.add(capsfilter)
        thumbnailsink.add(self.gdkpixbufsink)
        capsfilter.link(self.gdkpixbufsink)
        sinkpad = Gst.GhostPad.new(name="sink", target=(thumbnailsink.find_unlinked_pad(Gst.PadDirection.SINK)))
        thumbnailsink.add_pad(sinkpad)

        # Connect the playbin and the thumbnailsink
        self.pipeline.props.video_sink = thumbnailsink

        self.pipeline.set_state(Gst.State.PAUSED)
        # Wait for the pipeline to be prerolled so we can check the width
        # that the thumbnails will have and set the aspect ratio accordingly
        # as well as getting the framerate of the video:
        change_return = self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
        if Gst.StateChangeReturn.SUCCESS == change_return[0]:
            neg_caps = sinkpad.get_current_caps()[0]
            self.thumb_width = neg_caps["width"]
            self.framerate = neg_caps["framerate"]
        else:
            # the pipeline couldn't be prerolled so we can't determine the
            # correct values. Set sane defaults (this should never happen)
            self.warning("Couldn't preroll the pipeline")
            self.thumb_width = 16 * self.thumb_height / 9 # assume 16:9 aspect ratio
            self.framerate = Gst.Fraction(24, 1) # assume 24fps

    def _addThumbnails(self):
        """
        Adds thumbnails for the whole clip.

        Takes the zoom setting into account and removes potentially
        existing thumbnails prior to adding the new ones.
        """
        # TODO: check if duration or zoomratio really changed?
        self.remove_all_children()
        self.thumbs = {}

        # calculate unquantized length of a thumb in nano seconds
        thumb_duration_tmp = Zoomable.pixelToNs(self.thumb_width + self.thumb_margin)

        # quantize thumb length to thumb_period
        # TODO: replace with a call to utils.misc.quantize:
        thumb_duration = (thumb_duration_tmp // self.thumb_period) * self.thumb_period
        # make sure that the thumb duration after the quantization isn't smaller than before
        if thumb_duration < thumb_duration_tmp:
            thumb_duration += self.thumb_period

        # make sure that we don't show thumbnails more often than thumb_period
        thumb_duration = max(thumb_duration, self.thumb_period)

        number_of_thumbs = self.duration / thumb_duration

        current_time = 0
        for i in range(0, number_of_thumbs):
            thumb = Thumbnail(self.thumb_width, self.thumb_height)
            thumb.set_position(Zoomable.nsToPixel(current_time), self.thumb_margin)
            self.add_child(thumb)
            self.thumbs[current_time] = thumb
            self._thumbForTime(current_time)
            current_time += thumb_duration

    def _thumbForTime(self, time):
        if time in self.thumb_cache:
            gdkpixbuf = self.thumb_cache[time]
            self.thumbs[time].set_from_gdkpixbuf(gdkpixbuf)
        else:
            self._requestThumbnail(time)

    def _requestThumbnail(self, time):
        """Queue a thumbnail request for the given time"""
        if time not in self.queue:# and len(self._queue) <= self.max_requests:
            if self.queue:
                self.queue.append(time)
            else:
                self.queue.append(time)
                self._nextThumbnail()

    def _nextThumbnail(self):
        """Notifies the preview object that the pipeline is ready to process
        the next thumbnail in the queue. This should always be called from the
        main application thread."""
        if self.queue:
            if not self._startThumbnail(self.queue[0]):
                self.queue.pop(0)
                self._nextThumbnail()
        return False

    def _startThumbnail(self, time):
        self.waiting_timestamp = time
        return self.pipeline.seek(1.0,
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET, time,
            Gst.SeekType.NONE, -1)

    def _finishThumbnail(self, gdkpixbuf, time):
        """Notifies the preview object that the a new thumbnail is ready to be
        cached. This should be called by subclasses when they have finished
        processing the thumbnail for the current segment. This function should
        always be called from the main thread of the application."""
        waiting = self.waiting_timestamp
        self.waiting_timestamp = None

        if time != waiting:
            time = waiting

        self.thumb_cache[time] = gdkpixbuf

        if time in self.thumbs:
            self.thumbs[time].set_from_gdkpixbuf(gdkpixbuf)
        #self.emit("update", time)

        if time in self.queue:
            self.queue.remove(time)
        self._nextThumbnail()
        return False

    # Interface (Zoomable)

    def _maybeUpdate(self):
        self._addThumbnails()
        self.callback_id = None
        return False

    def zoomChanged(self):
        if self.callback_id is not None:
            GObject.source_remove(self.callback_id)
        self.callback_id = GObject.timeout_add(100, self._maybeUpdate)

    # Callbacks

    def bus_message_handler(self, unused_bus, message):
        # set the scaling method of the videoscale element to Lanczos
        #element = message.src
        #if isinstance(element, Gst.Element):
        #    factory = element.get_factory()
        #    if factory and "GstVideoScale" == factory.get_element_type().name:
        #        element.props.method = 3
        if message.type == Gst.MessageType.ELEMENT and \
                message.src == self.gdkpixbufsink:
            struct = message.get_structure()

            # TODO: does struct.get_name() work?
            #if struct.get_name() == "pixbuf":

            # TODO: there exists no value named "timestamp"
            #self._finishThumbnail(struct.get_value("pixbuf"), struct.get_value("timestamp"))
            GLib.idle_add(self._finishThumbnail, struct.get_value("pixbuf"),
                    struct.get_value("timestamp"))
        return Gst.BusSyncReply.PASS

    #bElement = receiver()

    #@handler(bElement, "notify::duration")
    #@handler(bElement, "notify::in-point")
    def element_changed(self, unused_bElement, unused_start_duration):
        self.duration = self.bElement.get_duration()
        self.in_point = self.bElement.get_in_point()
        GLib.idle_add(self._addThumbnails)

class Thumbnail(Clutter.Actor):

    def __init__(self, width, height):
        Clutter.Actor.__init__(self)
        image = Clutter.Image.new()
        self.props.content = image
        self.width = width
        self.height = height
        self.set_background_color(Clutter.Color.new(0, 100, 150, 100))
        self.set_size(self.width, self.height)

    def set_from_gdkpixbuf(self, gdkpixbuf):
        row_stride = gdkpixbuf.get_rowstride()
        pixel_data = gdkpixbuf.get_pixels()
        # Cogl.PixelFormat.RGB_888 := 2
        self.props.content.set_data(pixel_data, Cogl.PixelFormat.RGB_888, self.width, self.height, row_stride)

# TODO: replace with utils.misc.hash_file
def hash_file(uri):
    """Hashes the first 256KB of the specified file"""
    sha256 = hashlib.sha256()
    with open(uri, "rb") as file:
        for _ in range(1024):
            chunk = file.read(256)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()

# TODO: remove eventually
autocreate = True

# TODO: replace with pitivi.settings.get_dir
def get_dir(path, autocreate=True):
    if autocreate and not os.path.exists(path):
        os.makedirs(path)
    return path

class ThumbnailCache(object):

    """Caches thumbnails by key using LRU policy, implemented with heapq.

    Uses a two stage caching mechanism. A limited number of elements are
    held in memory, the rest is being cached on disk using an sqlite db."""

    def __init__(self, uri):
        object.__init__(self)
        # TODO: replace with utils.misc.hash_file
        self.hash = hash_file(Gst.uri_get_location(uri))
        # TODO: replace with pitivi.settings.xdg_cache_home()
        cache_dir = get_dir(os.path.join(xdg_dirs.xdg_cache_home, "pitivi"), autocreate)
        dbfile = os.path.join(get_dir(os.path.join(cache_dir, "thumbs")), self.hash)
        self.conn = sqlite3.connect(dbfile)
        self.cur = self.conn.cursor()
        self.cur.execute("CREATE TABLE IF NOT EXISTS Thumbs (Time INTEGER NOT NULL PRIMARY KEY,\
            Data BLOB NOT NULL, Width INTEGER NOT NULL, Height INTEGER NOT NULL, Stride INTEGER NOT NULL)")

    def __contains__(self, key):
        # check if item is present in on disk cache
        self.cur.execute("SELECT Time FROM Thumbs WHERE Time = ?", (key,))
        if self.cur.fetchone():
            return True
        return False

    def __getitem__(self, key):
        self.cur.execute("SELECT * FROM Thumbs WHERE Time = ?", (key,))
        row = self.cur.fetchone()
        if row:
            pixbuf = GdkPixbuf.Pixbuf.new_from_data(row[1],
                                                    GdkPixbuf.Colorspace.RGB,
                                                    False,
                                                    8,
                                                    row[2],
                                                    row[3],
                                                    row[4],
                                                    None,
                                                    None)
            return pixbuf
        raise KeyError(key)

    def __setitem__(self, key, value):
        blob = sqlite3.Binary(bytearray(value.get_pixels()))
        #Replace if the key already existed
        self.cur.execute("DELETE FROM Thumbs WHERE  time=?", (key,))
        self.cur.execute("INSERT INTO Thumbs VALUES (?,?,?,?,?)", (key, blob, value.get_width(), value.get_height(), value.get_rowstride()))
        self.conn.commit()

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
