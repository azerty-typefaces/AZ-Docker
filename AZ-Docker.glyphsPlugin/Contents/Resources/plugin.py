"""AZ Docker — dock floating windows into the Glyphs 4 Edit-window left sidebar."""

import contextlib
import functools
import json
import traceback
from pathlib import Path

import objc
from AppKit import (
    NSAnimationContext,
    NSApp,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSBezelStyleDisclosure,
    NSBezierPath,
    NSBox,
    NSBoxCustom,
    NSButton,
    NSButtonTypeOnOff,
    NSColor,
    NSColorSpace,
    NSCursor,
    NSEvenOddWindingRule,
    NSEvent,
    NSEventMaskLeftMouseUp,
    NSFont,
    NSGradient,
    NSGraphicsContext,
    NSImage,
    NSLayoutAttributeWidth,
    NSLineBreakByTruncatingTail,
    NSMakeSize,
    NSNoBorder,
    NSNoTitle,
    NSPanel,
    NSScrollView,
    NSStackView,
    NSTextField,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSViewController,
    NSViewHeightSizable,
    NSViewMaxXMargin,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindowController,
    NSWindowDidBecomeKeyNotification,
    NSWindowDidBecomeMainNotification,
    NSWindowDidMoveNotification,
    NSWindowStyleMaskResizable,
    NSWindowWillCloseNotification,
)
from Foundation import (
    NSInsetRect,
    NSMakeRect,
    NSNotificationCenter,
    NSObject,
    NSPointInRect,
)
from GlyphsApp import DOCUMENTOPENED, Glyphs
from GlyphsApp.plugins import GeneralPlugin

from dock_logic import (
    CandidateInfo,
    DockedEntry,
    DockStore,
    height_resizable,
    is_candidate,
)

# Sidebar style tokens shared by the AZ plugins.
AZ_CARD_RADIUS = 8.0  # rounded corner of a sidebar card
AZ_CHIP_RADIUS = 6.0  # inner chips, pills and grid cells
AZ_SIDE_MARGIN = 10.0  # sidebar edge -> card
AZ_CARD_PAD_X = 8.0  # card edge -> content, horizontal
AZ_CARD_PAD_Y = 6.0  # card edge -> content, vertical
AZ_CARD_GAP = 6.0  # between stacked cards
AZ_BAND_H = 24.0  # card header band height
AZ_TEXT_PT = 11.0  # body and (bold) header type
AZ_METRIC_PT = 9.0  # monospaced metric / caption type
AZ_TAB_ICON_H = 17.6  # sidebar tab-bar icon height (16 + 10%)
AZ_CARD_RGB = 40 / 255.0  # house card grey, ALWAYS declared in sRGB
# Dark mode: text on the card grey is pinned white.
AZ_INK = 0.95  # primary card text
AZ_INK_DIM = 0.62  # captions, counts, secondary card text
AZ_ICON_FILE = "az-icon.pdf"

_TAB_TITLE = "AZ Docker"
GRIP_HEIGHT = 8.0
MIN_CONTENT_HEIGHT = 40.0
MAX_CONTENT_HEIGHT = 2000.0
OFFSCREEN = (-20000.0, -20000.0)
DEFAULTS_KEY = "com.hugojourdan.AZ-Docker.dockedTitles"

# {title: content height or None}. Only an explicit undock removes a title, so a
# remembered window re-docks whenever it reappears, across relaunches too.
_sticky = {}
_sidebar_registered = False


def _az_ink():
    return NSColor.whiteColor().colorWithAlphaComponent_(AZ_INK)


def _az_ink_dim():
    return NSColor.whiteColor().colorWithAlphaComponent_(AZ_INK_DIM)


def _az_card_fill():
    # sRGB, never calibrated: a calibrated 40 paints as sRGB ~53
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        AZ_CARD_RGB, AZ_CARD_RGB, AZ_CARD_RGB, 1.0
    )


def _is_dark(appearance) -> bool:
    best = appearance.bestMatchFromAppearancesWithNames_(
        [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
    )
    return best == NSAppearanceNameDarkAqua


def _adaptive(light, dark):
    """Native window colour in light mode, the AZ card colour in dark mode."""
    return NSColor.colorWithName_dynamicProvider_(
        None, lambda appearance: dark if _is_dark(appearance) else light
    )


@functools.cache
def _tab_image():
    path = Path(__file__).with_name(AZ_ICON_FILE)
    image = NSImage.alloc().initWithContentsOfFile_(str(path))
    image.setTemplate_(True)
    image.setSize_(NSMakeSize(AZ_TAB_ICON_H, AZ_TAB_ICON_H))
    return image


def _draw_glass_border(rect, radius):
    # 1pt rim lit at a fixed 45°: light -> card colour -> light along the diagonal
    outer = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius
    )
    inner = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSInsetRect(rect, 1.0, 1.0), max(0.0, radius - 1.0), max(0.0, radius - 1.0)
    )
    ring = NSBezierPath.bezierPath()
    ring.appendBezierPath_(outer)
    ring.appendBezierPath_(inner)
    ring.setWindingRule_(NSEvenOddWindingRule)
    context = NSGraphicsContext.currentContext()
    if context is None:
        return
    context.saveGraphicsState()
    try:
        ring.addClip()
        light = NSColor.whiteColor().colorWithAlphaComponent_(0.32)
        dark = _az_card_fill()
        gradient = NSGradient.alloc().initWithColors_atLocations_colorSpace_(
            [light, dark, dark, light],
            [0.0, 0.375, 0.625, 1.0],
            NSColorSpace.genericRGBColorSpace(),
        )
        gradient.drawInRect_angle_(rect, 45.0 if bool(context.isFlipped()) else 135.0)
    finally:
        context.restoreGraphicsState()


def _load_sticky_titles():
    global _sticky
    try:
        raw = Glyphs.defaults[DEFAULTS_KEY]
        _sticky = {
            str(title): (float(height) if height else None)
            for title, height in (json.loads(raw) if raw else {}).items()
        }
    except Exception:
        _sticky = {}


def _save_sticky_titles():
    try:
        Glyphs.defaults[DEFAULTS_KEY] = json.dumps(_sticky, sort_keys=True)
    except Exception:
        traceback.print_exc()


def _grab_point():
    """Top-left screen point that puts a window's title bar under the cursor."""
    location = NSEvent.mouseLocation()
    return (location.x - 60.0, location.y + 12.0)


class AZDockerCardBox(NSBox):
    def drawRect_(self, dirty):
        objc.super(AZDockerCardBox, self).drawRect_(dirty)
        if _is_dark(self.effectiveAppearance()):
            _draw_glass_border(self.bounds(), AZ_CARD_RADIUS)
            return
        NSColor.separatorColor().setStroke()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(self.bounds(), 0.5, 0.5),
            AZ_CARD_RADIUS - 0.5,
            AZ_CARD_RADIUS - 0.5,
        ).stroke()


class AZDockerStackView(NSStackView):
    def isFlipped(self):
        return True  # pins the cards to the top of the scroll view


class DockUI:
    """The one stack hierarchy, adopted by whichever sidebar tab is visible."""

    def __init__(self):
        self.store = DockStore()
        self.root = None
        self.stack = None
        self.hint = None
        self.host = None
        self.highlight = None
        self.out_highlight = None
        self._entry_views = {}

    def ensure_built(self):
        if self.root is not None:
            return
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 400))

        scroll = NSScrollView.alloc().initWithFrame_(root.bounds())
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(NSNoBorder)
        if scroll.respondsToSelector_("setAllowedPocketEdges:"):
            scroll.setAllowedPocketEdges_(0)  # macOS 26 scroll pocket blurs top content
        root.addSubview_(scroll)

        stack = AZDockerStackView.alloc().initWithFrame_(root.bounds())
        stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        stack.setAlignment_(NSLayoutAttributeWidth)
        stack.setSpacing_(AZ_CARD_GAP)
        stack.setEdgeInsets_(
            (AZ_SIDE_MARGIN, AZ_SIDE_MARGIN, AZ_SIDE_MARGIN, AZ_SIDE_MARGIN)
        )
        stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        scroll.setDocumentView_(stack)
        clip = scroll.contentView()
        stack.topAnchor().constraintEqualToAnchor_(clip.topAnchor()).setActive_(True)
        stack.leadingAnchor().constraintEqualToAnchor_(clip.leadingAnchor()).setActive_(
            True
        )
        stack.widthAnchor().constraintEqualToAnchor_(clip.widthAnchor()).setActive_(
            True
        )

        hint = NSTextField.labelWithString_("Drag a floating window here")
        hint.setTextColor_(NSColor.secondaryLabelColor())
        hint.setFont_(NSFont.systemFontOfSize_(AZ_TEXT_PT))
        hint.sizeToFit()
        hint_size = hint.frame().size
        root_bounds = root.bounds()
        hint.setFrameOrigin_(
            (
                (root_bounds.size.width - hint_size.width) / 2.0,
                (root_bounds.size.height - hint_size.height) / 2.0,
            )
        )
        hint.setAutoresizingMask_(
            NSViewMinXMargin | NSViewMaxXMargin | NSViewMinYMargin | NSViewMaxYMargin
        )
        root.addSubview_(hint)

        highlight = NSView.alloc().initWithFrame_(root.bounds())
        highlight.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        highlight.setWantsLayer_(True)
        highlight.layer().setBorderWidth_(5.0)
        highlight.layer().setBorderColor_(NSColor.controlAccentColor().CGColor())
        highlight.layer().setCornerRadius_(4.0)
        highlight.setHidden_(True)
        root.addSubview_(highlight)

        self.root, self.stack = root, stack
        self.hint, self.highlight = hint, highlight

    def adopt(self, container):
        self.ensure_built()
        if self.root.superview() is not None:
            self.root.removeFromSuperview()
        self.root.setFrame_(container.bounds())
        self.root.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        container.addSubview_(self.root)
        self.host = container

    def release_host(self, container):
        # tab B can appear before tab A disappears: only release our own host
        if self.host == container:
            self.root.removeFromSuperview()
            self.host = None

    def screen_drop_rect(self):
        """Drop zone in screen coordinates, or None while the tab is hidden."""
        if self.host is None:
            return None
        window = self.root.window()
        if window is None:
            return None
        rect_in_window = self.root.convertRect_toView_(self.root.bounds(), None)
        return window.convertRectToScreen_(rect_in_window)

    def _edit_canvas_view(self):
        if self.host is None:
            return None
        window = self.host.window()
        return window.contentView() if window is not None else None

    def set_drop_highlight(self, zone):
        """'in' strokes the sidebar, 'out' strokes the Edit view, None clears both."""
        try:
            if self.highlight is not None:
                self.highlight.setHidden_(zone != "in")
            canvas = self._edit_canvas_view() if zone == "out" else None
            if canvas is not None:
                if self.out_highlight is None:
                    overlay = NSView.alloc().initWithFrame_(canvas.bounds())
                    overlay.setAutoresizingMask_(
                        NSViewWidthSizable | NSViewHeightSizable
                    )
                    overlay.setWantsLayer_(True)
                    overlay.layer().setBorderWidth_(5.0)
                    overlay.layer().setBorderColor_(
                        NSColor.controlAccentColor().CGColor()
                    )
                    self.out_highlight = overlay
                if self.out_highlight.superview() != canvas:
                    self.out_highlight.removeFromSuperview()
                    self.out_highlight.setFrame_(canvas.bounds())
                    canvas.addSubview_(self.out_highlight)
                self.out_highlight.setHidden_(False)
            elif self.out_highlight is not None:
                self.out_highlight.setHidden_(True)
        except Exception:
            traceback.print_exc()

    def rebuild(self):
        self.ensure_built()
        for view in list(self.stack.arrangedSubviews()):
            self.stack.removeArrangedSubview_(view)
            view.removeFromSuperview()
        self._entry_views = {}
        self.hint.setHidden_(bool(self.store.entries))
        for entry in self.store.entries:
            header = AZDockerHeaderView.alloc().initWithWindow_title_collapsed_(
                entry.window, entry.title, entry.collapsed
            )
            holder = NSView.alloc().initWithFrame_(
                NSMakeRect(0, 0, 240, entry.content_height + 4.0)
            )
            holder.setTranslatesAutoresizingMaskIntoConstraints_(False)
            height_constraint = holder.heightAnchor().constraintEqualToConstant_(
                entry.content_height + 4.0
            )
            height_constraint.setActive_(True)
            # pinned with constraints so the content follows the sidebar width live
            content = entry.content_view
            content.setTranslatesAutoresizingMaskIntoConstraints_(False)
            holder.addSubview_(content)
            content.topAnchor().constraintEqualToAnchor_(holder.topAnchor()).setActive_(
                True
            )
            content.leadingAnchor().constraintEqualToAnchor_constant_(
                holder.leadingAnchor(), 4.0
            ).setActive_(True)
            content.trailingAnchor().constraintEqualToAnchor_constant_(
                holder.trailingAnchor(), -4.0
            ).setActive_(True)
            content.bottomAnchor().constraintEqualToAnchor_constant_(
                holder.bottomAnchor(), -4.0
            ).setActive_(True)
            content.setNeedsDisplay_(True)
            holder.setHidden_(entry.collapsed)
            grip = None
            if entry.height_resizable:
                grip = AZDockerResizeGripView.alloc().initWithWindow_constraint_(
                    entry.window, height_constraint
                )
                grip.setHidden_(entry.collapsed)
            card = self._make_card(header, holder, grip)
            self.stack.addArrangedSubview_(card)
            self._entry_views[entry.window] = (card, header, holder, grip)

    def _make_card(self, header, holder, grip):
        card = AZDockerCardBox.alloc().initWithFrame_(NSMakeRect(0, 0, 240, 100))
        card.setBoxType_(NSBoxCustom)
        card.setTitlePosition_(NSNoTitle)
        card.setCornerRadius_(AZ_CARD_RADIUS)
        card.setBorderWidth_(0.0)  # the glass rim replaces the box border
        card.setFillColor_(_adaptive(NSColor.windowBackgroundColor(), _az_card_fill()))
        card.setContentViewMargins_((0.0, 0.0))
        card.setTranslatesAutoresizingMaskIntoConstraints_(False)

        inner = NSStackView.alloc().initWithFrame_(card.bounds())
        inner.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        inner.setAlignment_(NSLayoutAttributeWidth)
        inner.setSpacing_(0.0)
        inner.setTranslatesAutoresizingMaskIntoConstraints_(False)
        inner.addArrangedSubview_(header)
        inner.addArrangedSubview_(holder)
        if grip is not None:
            inner.addArrangedSubview_(grip)

        box_content = card.contentView()
        box_content.addSubview_(inner)
        inner.topAnchor().constraintEqualToAnchor_(box_content.topAnchor()).setActive_(
            True
        )
        inner.bottomAnchor().constraintEqualToAnchor_(
            box_content.bottomAnchor()
        ).setActive_(True)
        inner.leadingAnchor().constraintEqualToAnchor_(
            box_content.leadingAnchor()
        ).setActive_(True)
        inner.trailingAnchor().constraintEqualToAnchor_(
            box_content.trailingAnchor()
        ).setActive_(True)
        return card

    def apply_collapse(self, window):
        entry = self.store.find(window)
        views = self._entry_views.get(window)
        if entry is None or views is None:
            return
        _, header, holder, grip = views
        holder.setHidden_(entry.collapsed)
        if grip is not None:
            grip.setHidden_(entry.collapsed)
        header.disclosure.setState_(0 if entry.collapsed else 1)
        header.setNeedsDisplay_(True)  # band corners depend on the state

    def move_entry(self, from_index, to_index):
        self.store.move(from_index, to_index)
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(0.18)
        NSAnimationContext.currentContext().setAllowsImplicitAnimation_(True)
        self._resync_order()
        self.stack.layoutSubtreeIfNeeded()
        NSAnimationContext.endGrouping()

    def _resync_order(self):
        # re-inserting MOVES the card, so the dragged header keeps its mouse session
        for index, entry in enumerate(self.store.entries):
            views = self._entry_views.get(entry.window)
            if views is not None:
                self.stack.insertArrangedSubview_atIndex_(views[0], index)

    def entry_index_at_y(self, y, fallback):
        for index, entry in enumerate(self.store.entries):
            views = self._entry_views.get(entry.window)
            if views is None:
                continue
            frame = views[0].frame()
            if frame.origin.y <= y <= frame.origin.y + frame.size.height:
                return index
        return fallback


_ui = DockUI()


class AZDockerHeaderView(NSView):
    """Card header: disclosure, title, ✕. Drag to reorder, or out to undock."""

    def initWithWindow_title_collapsed_(self, window, title, collapsed):
        self = objc.super(AZDockerHeaderView, self).initWithFrame_(
            NSMakeRect(0, 0, 240, AZ_BAND_H)
        )
        if self is None:
            return None
        self.dock_window = window
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.heightAnchor().constraintEqualToConstant_(AZ_BAND_H).setActive_(True)

        disclosure = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 18, 18))
        disclosure.setButtonType_(NSButtonTypeOnOff)
        disclosure.setBezelStyle_(NSBezelStyleDisclosure)
        disclosure.setTitle_("")
        disclosure.setState_(0 if collapsed else 1)
        disclosure.setTarget_(self)
        disclosure.setAction_("toggleCollapse:")
        disclosure.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.addSubview_(disclosure)
        self.disclosure = disclosure

        label = NSTextField.labelWithString_(title)
        label.setFont_(NSFont.boldSystemFontOfSize_(AZ_TEXT_PT))
        label.setTextColor_(_adaptive(NSColor.labelColor(), _az_ink()))
        label.setLineBreakMode_(NSLineBreakByTruncatingTail)
        label.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.addSubview_(label)

        close = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 20, 20))
        close.setBordered_(False)
        close.setTitle_("✕")
        close.setFont_(NSFont.systemFontOfSize_(AZ_TEXT_PT))
        close.setContentTintColor_(_adaptive(NSColor.labelColor(), _az_ink()))
        close.setTarget_(self)
        close.setAction_("undockClicked:")
        close.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.addSubview_(close)

        center_y = self.centerYAnchor()
        disclosure.leadingAnchor().constraintEqualToAnchor_constant_(
            self.leadingAnchor(), 6.0
        ).setActive_(True)
        disclosure.centerYAnchor().constraintEqualToAnchor_(center_y).setActive_(True)
        label.leadingAnchor().constraintEqualToAnchor_constant_(
            disclosure.trailingAnchor(), 4.0
        ).setActive_(True)
        label.centerYAnchor().constraintEqualToAnchor_(center_y).setActive_(True)
        label.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(
            close.leadingAnchor(), -4.0
        ).setActive_(True)
        close.trailingAnchor().constraintEqualToAnchor_constant_(
            self.trailingAnchor(), -6.0
        ).setActive_(True)
        close.centerYAnchor().constraintEqualToAnchor_(center_y).setActive_(True)
        return self

    def drawRect_(self, rect):
        # darker band; expanded rounds only the top corners, collapsed all four
        try:
            bounds = self.bounds()
            if self.disclosure.state() == 0:
                band = bounds
            else:
                band = NSMakeRect(
                    0.0, -10.0, bounds.size.width, bounds.size.height + 10.0
                )
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                band, 7.0, 7.0
            )
            _adaptive(
                NSColor.whiteColor(),
                NSColor.blackColor().colorWithAlphaComponent_(0.15),
            ).setFill()
            path.fill()
        except Exception:
            traceback.print_exc()

    def toggleCollapse_(self, sender):
        try:
            _ui.store.toggle_collapsed(self.dock_window)
            _ui.apply_collapse(self.dock_window)
        except Exception:
            traceback.print_exc()

    def undockClicked_(self, sender):
        # hides the window; its owner plugin reopens it later
        try:
            undock_window(self.dock_window, order_front=False)
        except Exception:
            traceback.print_exc()

    def mouseDown_(self, event):
        try:
            NSCursor.closedHandCursor().set()
        except Exception:
            traceback.print_exc()

    def mouseDragged_(self, event):
        try:
            NSCursor.closedHandCursor().set()
            window = self.dock_window
            point_in_root = _ui.root.convertPoint_fromView_(
                event.locationInWindow(), None
            )
            inside = NSPointInRect(
                point_in_root, NSInsetRect(_ui.root.bounds(), -30.0, -30.0)
            )
            current = _ui.store.index_of(window)
            if current is None:
                return  # already handed to the window server below
            if inside:
                _ui.set_drop_highlight("in")
                point = _ui.stack.convertPoint_fromView_(event.locationInWindow(), None)
                target = _ui.entry_index_at_y(point.y, current)
                if target != current:
                    _ui.move_entry(current, target)
                return
            # left the sidebar: float the window under the cursor and hand the drag
            # to the window server, which outlives rebuild() tearing this header down
            _ui.set_drop_highlight("out")
            undock_window(window, order_front=True, screen_point=_grab_point())
            if window.respondsToSelector_("performWindowDragWithEvent:"):
                window.performWindowDragWithEvent_(event)
        except Exception:
            traceback.print_exc()

    def mouseUp_(self, event):
        try:
            NSCursor.arrowCursor().set()
            _ui.set_drop_highlight(None)
        except Exception:
            traceback.print_exc()


class AZDockerResizeGripView(NSView):
    """Bottom strip of a card: drag to set the content height."""

    def initWithWindow_constraint_(self, window, constraint):
        self = objc.super(AZDockerResizeGripView, self).initWithFrame_(
            NSMakeRect(0, 0, 240, GRIP_HEIGHT)
        )
        if self is None:
            return None
        self.dock_window = window
        self.height_constraint = constraint
        self._drag_start_y = 0.0
        self._drag_start_constant = 0.0
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.heightAnchor().constraintEqualToConstant_(GRIP_HEIGHT).setActive_(True)
        return self

    def drawRect_(self, rect):
        try:
            bounds = self.bounds()
            handle = NSMakeRect(
                (bounds.size.width - 36.0) / 2.0,
                (bounds.size.height - 3.0) / 2.0,
                36.0,
                3.0,
            )
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                handle, 1.5, 1.5
            )
            _adaptive(NSColor.tertiaryLabelColor(), _az_ink_dim()).setFill()
            path.fill()
        except Exception:
            traceback.print_exc()

    def resetCursorRects(self):
        try:
            self.addCursorRect_cursor_(self.bounds(), NSCursor.resizeUpDownCursor())
        except Exception:
            traceback.print_exc()

    def mouseDown_(self, event):
        try:
            self._drag_start_y = event.locationInWindow().y
            self._drag_start_constant = self.height_constraint.constant()
        except Exception:
            traceback.print_exc()

    def mouseDragged_(self, event):
        try:
            # window coordinates grow upwards: dragging down grows the card
            delta = self._drag_start_y - event.locationInWindow().y
            new_height = max(
                MIN_CONTENT_HEIGHT + 4.0,
                min(MAX_CONTENT_HEIGHT, self._drag_start_constant + delta),
            )
            self.height_constraint.setConstant_(new_height)
            entry = _ui.store.find(self.dock_window)
            if entry is not None:
                entry.content_height = new_height - 4.0
        except Exception:
            traceback.print_exc()

    def mouseUp_(self, event):
        try:
            entry = _ui.store.find(self.dock_window)
            if entry is not None and entry.title in _sticky:
                _sticky[entry.title] = entry.content_height
                _save_sticky_titles()
        except Exception:
            traceback.print_exc()


def dock_window(window):
    if _ui.store.find(window) is not None:
        return
    content = window.contentView()
    if content is None:
        return
    entry = DockedEntry(
        window=window,
        content_view=content,
        frame=window.frame(),
        title=str(window.title() or "") or "Untitled",
        content_height=float(content.frame().size.height),
        height_resizable=height_resizable(
            bool(window.styleMask() & NSWindowStyleMaskResizable),
            window.minSize().height,
            window.maxSize().height,
        ),
    )
    window.setContentView_(NSView.alloc().initWithFrame_(content.frame()))
    # Stay ordered in, or panels stop refreshing (Kerning following the master),
    # but invisible and click-through. Alpha 0 also survives macOS pulling an
    # offscreen window back on screen.
    window.setAlphaValue_(0.0)
    window.setIgnoresMouseEvents_(True)
    if not (NSEvent.pressedMouseButtons() & 1):
        window.setFrameOrigin_(OFFSCREEN)  # mid-drag: _finish_drop parks it
    if window.isKeyWindow():
        main_window = NSApp.mainWindow()
        if main_window is not None:
            main_window.makeKeyWindow()
    stored_height = _sticky.get(entry.title)
    if stored_height and entry.height_resizable:
        entry.content_height = stored_height
    _ui.store.dock(entry)
    _sticky[entry.title] = entry.content_height
    _save_sticky_titles()
    _ui.rebuild()


def undock_window(window, order_front=True, forget=True, screen_point=None):
    """forget=False when the owner closes the window: it re-docks when reopened."""
    entry = _ui.store.undock(window)
    if entry is None:
        return
    content = entry.content_view
    if content.superview() is not None:
        content.removeFromSuperview()
    content.setTranslatesAutoresizingMaskIntoConstraints_(True)
    content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    window.setContentView_(content)
    window.setAlphaValue_(1.0)
    window.setIgnoresMouseEvents_(False)
    window.setFrame_display_(entry.frame, True)
    if forget:
        # before ordering front: DidBecomeKey would re-dock a remembered title
        _sticky.pop(entry.title, None)
        _save_sticky_titles()
    if order_front:
        if screen_point is not None:
            window.setFrameTopLeftPoint_(screen_point)
        window.makeKeyAndOrderFront_(None)
    else:
        window.orderOut_(None)
    _ui.rebuild()


try:
    _GS_DIALOG_CLASS = objc.lookUpClass("GSDialogController")
except objc.error:
    _GS_DIALOG_CLASS = None


def _is_glyphs_dialog(window) -> bool:
    # Glyphs' OK/Cancel dialogs, Filter menu included, run under a GSDialogController
    if _GS_DIALOG_CLASS is not None and any(
        owner is not None and owner.isKindOfClass_(_GS_DIALOG_CLASS)
        for owner in (window.windowController(), window.delegate())
    ):
        return True
    # Third-party filters may own their window without being its controller
    # (RMX Harmonizer). Only check UI that is already loaded.
    for plugin in Glyphs.filters:
        if plugin.isKindOfClass_(NSWindowController):
            if plugin.isWindowLoaded() and plugin.window() == window:
                return True
        elif plugin.isKindOfClass_(NSViewController) and plugin.isViewLoaded():
            view = plugin.view()
            if view is not None and view.window() == window:
                return True
    return False


def _candidate_info(window):
    window_controller = window.windowController()
    document = window_controller.document() if window_controller is not None else None
    return CandidateInfo(
        visible=bool(window.isVisible()),
        is_panel=bool(window.isKindOfClass_(NSPanel)),
        level=int(window.level()),
        has_content_view=window.contentView() is not None,
        is_document_window=document is not None,
        is_docked=_ui.store.find(window) is not None,
        has_title=bool(str(window.title() or "")),
        is_dialog=_is_glyphs_dialog(window),
    )


def _dock_if_candidate(window) -> bool:
    if window is not None and is_candidate(_candidate_info(window)):
        dock_window(window)
        return True
    return False


def _find_window_titled(title):
    for window in NSApp.windows():
        if str(window.title() or "") == title:
            return window
    return None


def _find_menu_item_titled(title):
    def walk(menu):
        for item in menu.itemArray():
            if "runFilterAction" in str(item.action()):
                continue  # running a filter would pop its dialog on font open
            if str(item.title() or "") == title:
                return menu, item
            submenu = item.submenu()
            if submenu is not None:
                found = walk(submenu)
                if found is not None:
                    return found
        return None

    main_menu = NSApp.mainMenu()
    return walk(main_menu) if main_menu is not None else None


def _restore_pending(run_menu_items):
    """Dock remembered windows that are open; optionally run the menu item of
    those that are not. Returns True while some are still unresolved."""
    docked = {entry.title for entry in _ui.store.entries}
    remaining = False
    for title in sorted(_sticky):
        if title in docked:
            continue
        try:
            window = _find_window_titled(title)
            if _dock_if_candidate(window):
                continue
            if window is None and run_menu_items:
                found = _find_menu_item_titled(title)
                if found is not None:
                    menu, item = found
                    menu.performActionForItemAtIndex_(menu.indexOfItem_(item))
                    # same runloop pass as the open, so it never reaches the screen
                    if _dock_if_candidate(_find_window_titled(title)):
                        continue
            remaining = True
        except Exception:
            traceback.print_exc()
    return remaining


def _finish_drop():
    _ui.set_drop_highlight(None)
    # a window docked mid-drag still sits at the drop point (at alpha 0)
    for entry in _ui.store.entries:
        try:
            entry.window.setFrameOrigin_(OFFSCREEN)
        except Exception:
            traceback.print_exc()


def _global_mouse_up(event):
    try:
        _finish_drop()
    except Exception:
        traceback.print_exc()


def _local_mouse_up(event):
    _global_mouse_up(event)
    return event


try:
    _SIDEBAR_PROTOCOL = objc.protocolNamed("GSEditViewSidebarControllerProtocol")
except Exception:  # absent before Glyphs 4
    _SIDEBAR_PROTOCOL = None


if _SIDEBAR_PROTOCOL is not None:

    class AZDockerSidebarController(NSViewController, protocols=[_SIDEBAR_PROTOCOL]):
        """One per Edit view; the visible one adopts the shared DockUI."""

        @classmethod
        def title(cls):
            return _TAB_TITLE

        @classmethod
        def tabBarImage(cls):
            return _tab_image()

        def loadView(self):
            container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 400))
            container.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            self.setView_(container)

        def viewDidAppear(self):
            objc.super(AZDockerSidebarController, self).viewDidAppear()
            try:
                _ui.adopt(self.view())
            except Exception:
                traceback.print_exc()

        def viewDidDisappear(self):
            objc.super(AZDockerSidebarController, self).viewDidDisappear()
            try:
                _ui.release_host(self.view())
            except Exception:
                traceback.print_exc()

        # strong reference: PyObjC cannot weak-store, one controller leaks per closed tab
        def controller(self):
            return getattr(self, "_gs_controller", None)

        def setController_(self, edit_controller):
            self.setTitle_(_TAB_TITLE)  # Glyphs shows the instance title as the tooltip
            self._gs_controller = edit_controller

else:
    AZDockerSidebarController = None


class AZDocker(GeneralPlugin):
    _live = None

    @objc.python_method
    def settings(self):
        self.name = "AZ Docker"

    def start(self):
        try:
            if AZDocker._live is not None:
                AZDocker._live._teardown()
            AZDocker._live = self
            self._monitors = []
            self._restore_menu_pending = False
            self._restore_attempts = 0
            if self._register_sidebar():
                _load_sticky_titles()
                self._install_observers()
                Glyphs.addCallback(self._document_opened, DOCUMENTOPENED)
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def _register_sidebar(self) -> bool:
        global _sidebar_registered
        handler = objc.lookUpClass("GSCallbackHandler")
        if AZDockerSidebarController is None or not hasattr(
            handler, "addEditViewLeftSidebarController_"
        ):
            print("AZ Docker: needs the Glyphs 4 left sidebar — plugin inactive.")
            return False
        if not _sidebar_registered:
            handler.addEditViewLeftSidebarController_(AZDockerSidebarController)
            _sidebar_registered = True
        return True

    @objc.python_method
    def _install_observers(self):
        center = NSNotificationCenter.defaultCenter()
        for selector, name in (
            ("azdockerWindowDidMove:", NSWindowDidMoveNotification),
            ("azdockerWindowWillClose:", NSWindowWillCloseNotification),
            ("azdockerWindowAppeared:", NSWindowDidBecomeKeyNotification),
            ("azdockerWindowAppeared:", NSWindowDidBecomeMainNotification),
        ):
            center.addObserver_selector_name_object_(self, selector, name, None)
        self._monitors = [
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskLeftMouseUp, _global_mouse_up
            ),
            NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskLeftMouseUp, _local_mouse_up
            ),
        ]

    def azdockerWindowDidMove_(self, notification):
        try:
            window = notification.object()
            drop_rect = _ui.screen_drop_rect()
            if drop_rect is None or not (NSEvent.pressedMouseButtons() & 1):
                return
            over_dock = bool(NSPointInRect(NSEvent.mouseLocation(), drop_rect))
            if _ui.store.find(window) is not None:
                # a window docked mid-drag still rides the drag: leaving pops it out
                _ui.set_drop_highlight("in" if over_dock else "out")
                if window.alphaValue() < 1.0 and not over_dock:
                    window.setAlphaValue_(1.0)
                    undock_window(window, order_front=True, screen_point=_grab_point())
                return
            if is_candidate(_candidate_info(window)):
                _ui.set_drop_highlight("in" if over_dock else None)
                if over_dock:
                    dock_window(window)  # docks while the mouse is still down
        except Exception:
            traceback.print_exc()

    @objc.python_method  # PyObjC 12 rejects plain (self, info) callbacks
    def _document_opened(self, info):
        try:
            self._restore_menu_pending = True
            self._restore_attempts = 0
            self._schedule_sweep(0.5)  # let the document finish opening first
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def _schedule_sweep(self, delay):
        NSObject.cancelPreviousPerformRequestsWithTarget_selector_object_(
            self, "azdockerRestorePending", None
        )
        self.performSelector_withObject_afterDelay_(
            "azdockerRestorePending", None, delay
        )

    def azdockerRestorePending(self):
        try:
            run_menu = self._restore_menu_pending
            self._restore_menu_pending = False
            remaining = _restore_pending(run_menu)
            self._restore_attempts += 1
            # windows opened asynchronously: rescan, never re-run menu items
            if remaining and self._restore_attempts < 5:
                self._schedule_sweep(0.7)
        except Exception:
            traceback.print_exc()

    def azdockerWindowAppeared_(self, notification):
        # a remembered window reappeared; dock it from a sweep, never mid-open
        try:
            window = notification.object()
            title = str(window.title() or "")
            if title in _sticky and _ui.store.find(window) is None:
                self._schedule_sweep(0.15)
        except Exception:
            traceback.print_exc()

    def azdockerWindowWillClose_(self, notification):
        # the owner is closing a docked window: hand its content back first
        try:
            window = notification.object()
            if _ui.store.find(window) is not None:
                undock_window(window, order_front=False, forget=False)
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def _teardown(self):
        with contextlib.suppress(Exception):
            NSNotificationCenter.defaultCenter().removeObserver_(self)
        with contextlib.suppress(Exception):
            Glyphs.removeCallback(self._document_opened)
        with contextlib.suppress(Exception):
            NSObject.cancelPreviousPerformRequestsWithTarget_(self)
        for monitor in self._monitors:
            with contextlib.suppress(Exception):
                NSEvent.removeMonitor_(monitor)
        self._monitors = []
