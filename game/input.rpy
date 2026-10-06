## Makes every text box in the game behave more like a normal one.
##
## Ren'Py's own text input throws away line breaks when pasting, and has no way to select
## everything. This wraps its key handling to add both:
##   - pasting into a box that allows several lines keeps the lines;
##   - Cmd+A or Ctrl+A selects all the text (shown in amber); typing, pasting, Backspace or Delete
##     then replaces it, Cmd/Ctrl+X cuts it, and any other key just drops the selection.
##
## It relies on the inside of renpy.display.behavior.Input, so it is the first place to look if
## text boxes misbehave after a Ren'Py update.

init python:
    import pygame_sdl2
    from renpy.display.behavior import Input as RenpyInput, map_event as renpy_map_event

    renpy_input_event = RenpyInput.event

    def clipboard_text():
        try:
            raw = pygame_sdl2.scrap.get(pygame_sdl2.scrap.SCRAP_TEXT)
        except Exception:
            return ""
        return (raw or b"").decode("utf-8", "replace")

    def show_selection(box, on):
        """Marks the whole content as selected, or puts the normal display with its caret back."""
        box.all_selected = on
        if on:
            box.set_text(["{color=#ffb347}" + box.content.replace("{", "{{") + "{/color}"])
        else:
            box.update_text(box.content, box.editable)
        renpy.display.render.redraw(box, 0)

    def better_input_event(self, ev, x, y, st):
        if not self.editable:
            return renpy_input_event(self, ev, x, y, st)

        key = ev.type == pygame_sdl2.KEYDOWN
        command = key and (ev.mod & (pygame_sdl2.KMOD_META | pygame_sdl2.KMOD_CTRL)) and not (ev.mod & pygame_sdl2.KMOD_ALT)
        pasting = self.copypaste and renpy_map_event(ev, "input_paste")

        if command and ev.key == pygame_sdl2.K_a:
            show_selection(self, bool(self.content))
            raise renpy.display.core.IgnoreEvent()

        if getattr(self, "all_selected", False):
            cutting = command and ev.key == pygame_sdl2.K_x
            deleting = renpy_map_event(ev, "input_backspace") or renpy_map_event(ev, "input_delete")
            if cutting and self.copypaste:
                pygame_sdl2.scrap.put(pygame_sdl2.scrap.SCRAP_TEXT, self.content.encode("utf-8"))
            if cutting or deleting or pasting or ev.type == pygame_sdl2.TEXTINPUT:
                # Whatever comes next replaces everything.
                self.all_selected = False
                self.caret_pos = 0
                self.update_text("", self.editable)
                if cutting or deleting:
                    raise renpy.display.core.IgnoreEvent()
            elif key and ev.key not in (pygame_sdl2.K_LMETA, pygame_sdl2.K_RMETA, pygame_sdl2.K_LCTRL, pygame_sdl2.K_RCTRL, pygame_sdl2.K_LSHIFT, pygame_sdl2.K_RSHIFT):
                show_selection(self, False)

        if pasting and self.multiline:
            # Ren'Py's own paste drops line breaks. Keep them, and turn tabs into spaces.
            text = clipboard_text().replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
            text = "".join(c for c in text if c == "\n" or ord(c) >= 32)
            if self.length:
                text = text[:max(0, self.length - len(self.content))]
            if text:
                content = self.content[:self.caret_pos] + text + self.content[self.caret_pos:]
                self.caret_pos += len(text)
                self.update_text(content, self.editable, check_size=True)
            raise renpy.display.core.IgnoreEvent()

        return renpy_input_event(self, ev, x, y, st)

    RenpyInput.event = better_input_event
