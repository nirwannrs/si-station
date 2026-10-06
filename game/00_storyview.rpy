## A scrolling view of the story that draws its text once and then only moves it.
##
## Ren'Py's own viewport lays out and draws every paragraph inside it again each time it scrolls,
## and so does any screen when something in it changes. With a long story that is a fifth of a
## second per notch of the mouse wheel. This view draws the story into one picture, keeps the
## picture, and on scrolling shows a different slice of it. The picture is made again only when
## what it shows has changed, which the screen says by giving a different version.

## "python early", and a file name that sorts first: a new screen-language statement has to
## exist before the files with the screens that use it are read.
python early:

    class StoryView(renpy.Displayable):

        latest = None

        def showing(self):
            """Whether the story is really drawn: there is a drawing, and Ren'Py has not thrown it away."""
            return self.kept is not None and not getattr(self.kept[1], "killed", False) and bool(getattr(self.kept[1], "children", True))

        def __init__(self, adjustment=None, version=None, step=120, where=None, **properties):
            super(StoryView, self).__init__(**properties)
            self.adjustment = adjustment or ui.adjustment()
            self.version = version
            self.step = step
            ## Where to open: a function giving how far down, in pixels, or None for the very end.
            ## Applied once, the first time this view is drawn. The screen makes a new view whenever
            ## what it shows changes, so "once" means once for each new turn or new size.
            self.where = where
            self.settled = False
            self.child = None
            self.kept = None            # (what it was drawn for, the drawing)
            self.size = (0, 0)
            StoryView.latest = self     # for the self-test, which checks that the drawing is really there

        def add(self, child):
            self.child = renpy.displayable(child)

        def visit(self):
            return [self.child] if self.child is not None else []

        def per_interact(self):
            self.adjustment.register(self)

        def render(self, width, height, st, at):
            wanted = (self.version, width, id(self.child))
            ## Ren'Py throws a drawing away once it has gone a while without being shown, which is
            ## what happens to this one whenever the menu is opened. A thrown-away drawing shows as
            ## nothing at all, so it is checked for before every use and made again if need be.
            if self.kept is None or self.kept[0] != wanted or getattr(self.kept[1], "killed", False):
                self.kept = (wanted, renpy.render(self.child, width, height, st, at))
            drawn = self.kept[1]
            self.size = (width, height)
            ## Changing an adjustment makes everything that uses it draw again, this view included,
            ## so nothing is set that is not actually different.
            adjustment = self.adjustment
            reach = max(0, drawn.height - height)
            if adjustment.range != reach:
                adjustment.range = reach
            if adjustment.page != height:
                adjustment.page = height
            at_y = adjustment.value
            if not self.settled:
                self.settled = True
                wanted_y = self.where() if self.where is not None else None
                at_y = reach if wanted_y is None else wanted_y
            at_y = max(0, min(at_y, reach))
            if adjustment.value != at_y:
                adjustment.value = at_y
            view = renpy.Render(width, height)
            view.blit(drawn.subsurface((0, int(self.adjustment.value), width, min(height, drawn.height))), (0, 0))
            return view

        def event(self, ev, x, y, st):
            if not (0 <= x < self.size[0] and 0 <= y < self.size[1]):
                return None
            move = -self.step if renpy.map_event(ev, "viewport_wheelup") else self.step if renpy.map_event(ev, "viewport_wheeldown") else 0
            if move:
                self.adjustment.change(max(0, min(self.adjustment.range, self.adjustment.value + move)))
                renpy.redraw(self, 0)
                raise renpy.IgnoreEvent()
            return None

    renpy.register_sl_displayable("storyview", StoryView, "", 1).add_property("adjustment").add_property("version").add_property("step").add_property("where").add_property_group("position")
