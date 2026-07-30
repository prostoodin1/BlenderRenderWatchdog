"""Rounded matte-glass Tk widgets used by Blender Render Watchdog."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable


DEFAULT_PALETTE = {
    "bg": "#070a12",
    "panel": "#111a2a",
    "panel_alt": "#18243a",
    "field": "#0b1220",
    "text": "#f7f9ff",
    "muted": "#9aa9c2",
    "soft": "#dce6f7",
    "accent": "#8b7cff",
    "accent_hot": "#aa9cff",
    "accent_blue": "#6ee7ff",
    "danger": "#ff6b8b",
    "line": "#2d3c58",
    "line_hot": "#7797c7",
    "shadow": "#03050a",
}


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def blend_hex(first: str, second: str, amount: float) -> str:
    """Blend two #RRGGBB colours. Kept pure so visual math is testable."""
    amount = clamp(amount)
    left = tuple(int(first[index : index + 2], 16) for index in (1, 3, 5))
    right = tuple(int(second[index : index + 2], 16) for index in (1, 3, 5))
    values = tuple(round(a + (b - a) * amount) for a, b in zip(left, right))
    return "#" + "".join(f"{value:02x}" for value in values)


def rounded_points(x1: float, y1: float, x2: float, y2: float, radius: float) -> list[float]:
    """Return a smooth-polygon path for a rounded rectangle."""
    radius = max(0.0, min(radius, abs(x2 - x1) / 2, abs(y2 - y1) / 2))
    return [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]


def rounded_rectangle(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, radius: float, **kwargs):
    return canvas.create_polygon(
        rounded_points(x1, y1, x2, y2, radius),
        smooth=True,
        splinesteps=32,
        **kwargs,
    )


class GlassButton(tk.Canvas):
    """A keyboard-accessible rounded button with animated hover states."""

    def __init__(
        self,
        parent,
        *,
        text: str = "",
        command: Callable[[], object] | None = None,
        style: str = "TButton",
        state: str = "normal",
        width: int | None = None,
        palette: dict[str, str] | None = None,
        backdrop: str | None = None,
        **kwargs,
    ) -> None:
        self.palette = {**DEFAULT_PALETTE, **(palette or {})}
        self.text = text
        self.command = command
        self.style_name = style
        self.widget_state = state
        self.selected = False
        self.hover_mix = 0.0
        self.focused = False
        self.pressed = False
        self._animation_serial = 0
        self._explicit_width = width
        self._height = 46 if "Primary" in style else 40
        if "Tab" in style:
            self._height = 42
        if "Chip" in style:
            self._height = 32
        requested_width = self._requested_width()
        super().__init__(
            parent,
            width=requested_width,
            height=self._height,
            background=backdrop or self.palette["panel"],
            borderwidth=0,
            highlightthickness=0,
            takefocus=1,
            cursor="hand2" if state != "disabled" else "arrow",
            **kwargs,
        )
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Enter>", lambda _event: self._animate_hover(1.0))
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self.bind("<Return>", lambda _event: self.invoke())
        self.bind("<space>", lambda _event: self.invoke())
        self.after_idle(self._draw)

    def _requested_width(self) -> int:
        if self._explicit_width is not None:
            return max(44, int(self._explicit_width) * 10 + 20)
        padding = 28 if "Tab" in self.style_name else 36
        return max(74, len(self.text) * 8 + padding)

    def _colours(self) -> tuple[str, str, str, str]:
        p = self.palette
        if self.widget_state == "disabled":
            return "#121a28", "#65738a", "#202b3f", "#121a28"
        if "Primary" in self.style_name:
            return p["accent"], "#080b13", p["accent_hot"], p["accent_blue"]
        if "Danger" in self.style_name:
            return "#442332", "#ffd9e2", "#613047", p["danger"]
        if "Tab" in self.style_name:
            if self.selected:
                return "#263554", p["text"], "#30446b", p["accent"]
            return "#121b2b", p["muted"], "#1d2a42", p["line_hot"]
        if "Chip" in self.style_name:
            return "#152136", p["soft"], "#1d2d48", p["accent_blue"]
        return "#1a263b", p["text"], "#263754", p["line_hot"]

    def _draw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        width = max(2, self.winfo_width())
        height = max(2, self.winfo_height())
        radius = min(16, (height - 4) / 2)
        base, foreground, hover, glow = self._colours()
        fill = blend_hex(base, hover, self.hover_mix)
        border = blend_hex(self.palette["line"], glow, max(self.hover_mix, 0.8 if self.focused else 0.0))
        shift = 1 if self.pressed else 0
        rounded_rectangle(self, 3, 5, width - 3, height - 1, radius, fill=self.palette["shadow"], outline="")
        rounded_rectangle(self, 2, 2 + shift, width - 2, height - 4 + shift, radius, fill=border, outline="")
        rounded_rectangle(self, 3, 3 + shift, width - 3, height - 5 + shift, max(1, radius - 1), fill=fill, outline="")
        self.create_line(
            radius + 3,
            4 + shift,
            width - radius - 3,
            4 + shift,
            fill=blend_hex(fill, "#ffffff", 0.18 + self.hover_mix * 0.08),
            width=1,
        )
        font_size = 10 if "Chip" not in self.style_name else 9
        self.create_text(
            width / 2,
            (height - 2) / 2 + shift,
            text=self.text,
            fill=foreground,
            font=("Segoe UI Variable Text", font_size, "bold"),
        )

    def _animate_hover(self, target: float) -> None:
        if self.widget_state == "disabled":
            return
        self._animation_serial += 1
        serial = self._animation_serial

        def step() -> None:
            if serial != self._animation_serial or not self.winfo_exists():
                return
            distance = target - self.hover_mix
            if abs(distance) < 0.035:
                self.hover_mix = target
                self._draw()
                return
            self.hover_mix += distance * 0.34
            self._draw()
            self.after(16, step)

        step()

    def _leave(self, _event=None) -> None:
        self.pressed = False
        self._animate_hover(0.0)

    def _press(self, _event=None) -> None:
        if self.widget_state != "disabled":
            self.pressed = True
            self._draw()

    def _release(self, event=None) -> None:
        was_pressed = self.pressed
        self.pressed = False
        self._draw()
        if was_pressed and event is not None and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self.invoke()

    def _focus_in(self, _event=None) -> None:
        self.focused = True
        self._draw()

    def _focus_out(self, _event=None) -> None:
        self.focused = False
        self._draw()

    def invoke(self):
        if self.widget_state != "disabled" and self.command:
            return self.command()
        return None

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self._animate_hover(0.45 if selected else 0.0)

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {}) if isinstance(cnf, dict) else {}
        options.update(kwargs)
        if "text" in options:
            self.text = str(options.pop("text"))
            super().configure(width=self._requested_width())
        if "state" in options:
            self.widget_state = str(options.pop("state"))
            super().configure(cursor="hand2" if self.widget_state != "disabled" else "arrow")
        if "command" in options:
            self.command = options.pop("command")
        if options:
            super().configure(**options)
        self._draw()

    config = configure

    def state(self, statespec: Iterable[str] | None = None):
        if statespec is None:
            return ("disabled",) if self.widget_state == "disabled" else ()
        for item in statespec:
            if item == "disabled":
                self.configure(state="disabled")
            elif item == "!disabled":
                self.configure(state="normal")


class GlassSwitch(tk.Canvas):
    """Rounded animated toggle replacing the classic square ttk checkbutton."""

    def __init__(
        self,
        parent,
        *,
        text: str,
        variable: tk.BooleanVar,
        command: Callable[[], object] | None = None,
        palette: dict[str, str] | None = None,
        backdrop: str | None = None,
        state: str = "normal",
        style: str | None = None,
        **kwargs,
    ) -> None:
        del style
        self.palette = {**DEFAULT_PALETTE, **(palette or {})}
        self.text = text
        self.variable = variable
        self.command = command
        self.widget_state = state
        self.position = 1.0 if variable.get() else 0.0
        self._animation_serial = 0
        width = max(88, 60 + len(text) * 7)
        super().__init__(
            parent,
            width=width,
            height=36,
            background=backdrop or self.palette["panel"],
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=1,
            **kwargs,
        )
        self.bind("<ButtonRelease-1>", lambda _event: self.invoke())
        self.bind("<space>", lambda _event: self.invoke())
        self.bind("<Return>", lambda _event: self.invoke())
        self.bind("<Configure>", lambda _event: self._draw())
        self.variable.trace_add("write", lambda *_args: self._animate_to(1.0 if self.variable.get() else 0.0))
        self.after_idle(self._draw)

    def _draw(self) -> None:
        self.delete("all")
        p = self.palette
        enabled = self.widget_state != "disabled"
        track = blend_hex("#273247", p["accent"], self.position) if enabled else "#1b2331"
        rounded_rectangle(self, 2, 7, 44, 29, 11, fill=track, outline="")
        thumb_x = 13 + 20 * self.position
        self.create_oval(thumb_x - 8, 10, thumb_x + 8, 26, fill="#f7f9ff" if enabled else "#65738a", outline="")
        self.create_text(
            56,
            18,
            text=self.text,
            anchor="w",
            fill=p["text"] if enabled else "#65738a",
            font=("Segoe UI Variable Text", 10, "bold"),
        )

    def _animate_to(self, target: float) -> None:
        self._animation_serial += 1
        serial = self._animation_serial

        def step() -> None:
            if serial != self._animation_serial or not self.winfo_exists():
                return
            distance = target - self.position
            if abs(distance) < 0.035:
                self.position = target
                self._draw()
                return
            self.position += distance * 0.36
            self._draw()
            self.after(16, step)

        step()

    def invoke(self):
        if self.widget_state == "disabled":
            return None
        self.variable.set(not self.variable.get())
        if self.command:
            return self.command()
        return None

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {}) if isinstance(cnf, dict) else {}
        options.update(kwargs)
        if "state" in options:
            self.widget_state = str(options.pop("state"))
        if "text" in options:
            self.text = str(options.pop("text"))
        if options:
            super().configure(**options)
        self._draw()

    config = configure


class GlassEntry(tk.Canvas):
    """A real Tk entry embedded in a rounded focus ring."""

    def __init__(
        self,
        parent,
        *,
        textvariable=None,
        width: int | None = None,
        state: str = "normal",
        palette: dict[str, str] | None = None,
        backdrop: str | None = None,
        **kwargs,
    ) -> None:
        self.palette = {**DEFAULT_PALETTE, **(palette or {})}
        requested_width = 220 if width is None else max(68, width * 7 + 20)
        super().__init__(
            parent,
            width=requested_width,
            height=42,
            background=backdrop or self.palette["panel"],
            borderwidth=0,
            highlightthickness=0,
        )
        self.focused = False
        self.entry = tk.Entry(
            self,
            textvariable=textvariable,
            state=state,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            background=self.palette["field"],
            foreground=self.palette["text"],
            insertbackground=self.palette["text"],
            readonlybackground=self.palette["field"],
            disabledbackground="#111827",
            disabledforeground="#65738a",
            font=("Segoe UI Variable Text", 10),
            **kwargs,
        )
        self._window = self.create_window(13, 21, anchor="w", window=self.entry)
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        super().bind("<Configure>", self._resize)
        self.after_idle(self._draw)

    def _resize(self, _event=None) -> None:
        self.itemconfigure(self._window, width=max(20, self.winfo_width() - 26))
        self._draw()

    def _draw(self) -> None:
        self.delete("glass")
        border = self.palette["accent"] if self.focused else self.palette["line"]
        rounded_rectangle(self, 1, 2, self.winfo_width() - 1, 40, 13, fill=border, outline="", tags="glass")
        rounded_rectangle(self, 2, 3, self.winfo_width() - 2, 39, 12, fill=self.palette["field"], outline="", tags="glass")
        self.tag_lower("glass")

    def _focus_in(self, _event=None) -> None:
        self.focused = True
        self._draw()

    def _focus_out(self, _event=None) -> None:
        self.focused = False
        self._draw()

    def bind(self, sequence=None, func=None, add=None):
        if sequence == "<Configure>":
            return super().bind(sequence, func, add)
        return self.entry.bind(sequence, func, add)

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {}) if isinstance(cnf, dict) else {}
        options.update(kwargs)
        entry_options = {key: options.pop(key) for key in tuple(options) if key in {"state", "textvariable"}}
        if entry_options:
            self.entry.configure(**entry_options)
        if options:
            super().configure(**options)

    config = configure

    def get(self):
        return self.entry.get()

    def focus_set(self):
        return self.entry.focus_set()


class GlassCombo(GlassEntry):
    """Rounded shell around a readonly ttk combobox."""

    def __init__(
        self,
        parent,
        *,
        textvariable=None,
        values=(),
        width: int | None = None,
        state: str = "readonly",
        palette: dict[str, str] | None = None,
        backdrop: str | None = None,
        **kwargs,
    ) -> None:
        del kwargs
        self.palette = {**DEFAULT_PALETTE, **(palette or {})}
        requested_width = max(96, (width or 16) * 7 + 30)
        tk.Canvas.__init__(
            self,
            parent,
            width=requested_width,
            height=42,
            background=backdrop or self.palette["panel"],
            borderwidth=0,
            highlightthickness=0,
        )
        self.focused = False
        self.entry = ttk.Combobox(
            self,
            textvariable=textvariable,
            values=values,
            state=state,
            style="Glass.TCombobox",
            font=("Segoe UI Variable Text", 10),
        )
        self._window = self.create_window(12, 21, anchor="w", window=self.entry)
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        tk.Canvas.bind(self, "<Configure>", self._resize)
        self.after_idle(self._draw)


class GlassProgress(tk.Canvas):
    def __init__(self, parent, *, variable: tk.DoubleVar, maximum: float = 100.0, palette=None, backdrop=None, **kwargs) -> None:
        self.palette = {**DEFAULT_PALETTE, **(palette or {})}
        self.variable = variable
        self.maximum = maximum
        self.shimmer = 0.0
        self._shimmer_running = False
        super().__init__(
            parent,
            height=18,
            background=backdrop or self.palette["panel"],
            borderwidth=0,
            highlightthickness=0,
            **kwargs,
        )
        self.bind("<Configure>", lambda _event: self._draw())
        self.variable.trace_add("write", lambda *_args: self._value_changed())
        self.after_idle(self._draw)

    def _value_changed(self) -> None:
        self._draw()
        value = float(self.variable.get())
        if 0 < value < self.maximum and not self._shimmer_running:
            self._shimmer_running = True
            self.after(35, self._animate_shimmer)

    def _draw(self) -> None:
        self.delete("all")
        width = max(4, self.winfo_width())
        rounded_rectangle(self, 1, 3, width - 1, 15, 6, fill=self.palette["field"], outline="")
        ratio = clamp(float(self.variable.get()) / self.maximum if self.maximum else 0.0)
        fill_width = max(0.0, (width - 2) * ratio)
        if fill_width > 2:
            rounded_rectangle(
                self,
                1,
                3,
                fill_width,
                15,
                min(6, fill_width / 2),
                fill=self.palette["accent"],
                outline="",
            )
            shimmer_x = fill_width * self.shimmer
            if 5 < shimmer_x < fill_width - 3:
                self.create_line(shimmer_x, 5, shimmer_x + 7, 13, fill=self.palette["accent_blue"], width=2)

    def _animate_shimmer(self) -> None:
        value = float(self.variable.get())
        if not (0 < value < self.maximum) or not self.winfo_exists():
            self._shimmer_running = False
            self.shimmer = 0.0
            self._draw()
            return
        self.shimmer = (self.shimmer + 0.035) % 1.0
        self._draw()
        self.after(35, self._animate_shimmer)


class GlassWidgetFactory:
    """Proxy ttk while replacing legacy-shaped interactive controls."""

    def __init__(self, ttk_module, palette: dict[str, str]) -> None:
        self.ttk = ttk_module
        self.palette = palette

    def _backdrop(self, parent) -> str:
        try:
            style = str(parent.cget("style"))
        except (tk.TclError, AttributeError):
            style = ""
        if "SurfaceAlt" in style:
            return self.palette["panel_alt"]
        if "App" in style or "Top" in style:
            return self.palette["bg"]
        return self.palette["panel"]

    def Button(self, parent, **kwargs):
        return GlassButton(parent, palette=self.palette, backdrop=self._backdrop(parent), **kwargs)

    def Checkbutton(self, parent, **kwargs):
        return GlassSwitch(parent, palette=self.palette, backdrop=self._backdrop(parent), **kwargs)

    def Entry(self, parent, **kwargs):
        return GlassEntry(parent, palette=self.palette, backdrop=self._backdrop(parent), **kwargs)

    def Combobox(self, parent, **kwargs):
        return GlassCombo(parent, palette=self.palette, backdrop=self._backdrop(parent), **kwargs)

    def Progressbar(self, parent, **kwargs):
        kwargs.pop("mode", None)
        kwargs.pop("style", None)
        return GlassProgress(parent, palette=self.palette, backdrop=self._backdrop(parent), **kwargs)

    def __getattr__(self, name: str):
        return getattr(self.ttk, name)


class GlassCard(tk.Canvas):
    """Rounded panel with a matte fill, soft shadow and a real ttk content frame."""

    def __init__(self, parent, *, palette=None, padding: int = 18, radius: int = 24, backdrop=None, **kwargs) -> None:
        self.palette = {**DEFAULT_PALETTE, **(palette or {})}
        self.radius = radius
        self.inset = 11
        self.glow = 0.0
        self._pulse_serial = 0
        super().__init__(
            parent,
            height=100,
            background=backdrop or self.palette["bg"],
            borderwidth=0,
            highlightthickness=0,
            **kwargs,
        )
        inner_padding = max(5, padding - self.inset)
        self.content = ttk.Frame(self, style="GlassSurface.TFrame", padding=inner_padding)
        self.content._glass_shell = self
        self._window = self.create_window(self.inset, self.inset, anchor="nw", window=self.content)
        self.bind("<Configure>", self._resize)
        self.content.bind("<Configure>", self._content_resized, add="+")
        self.after_idle(self._content_resized)

    def _content_resized(self, _event=None) -> None:
        requested = max(70, self.content.winfo_reqheight() + self.inset * 2)
        if int(float(self.cget("height"))) != requested:
            super().configure(height=requested)

    def _resize(self, _event=None) -> None:
        self.itemconfigure(
            self._window,
            width=max(10, self.winfo_width() - self.inset * 2),
            height=max(10, self.winfo_height() - self.inset * 2),
        )
        self._draw()

    def _draw(self) -> None:
        self.delete("glass")
        width = max(4, self.winfo_width())
        height = max(4, self.winfo_height())
        border = blend_hex(self.palette["line"], self.palette["line_hot"], self.glow)
        rounded_rectangle(self, 5, 8, width - 3, height - 2, self.radius, fill=self.palette["shadow"], outline="", tags="glass")
        rounded_rectangle(self, 2, 2, width - 4, height - 7, self.radius, fill=border, outline="", tags="glass")
        rounded_rectangle(self, 3, 3, width - 5, height - 8, self.radius - 1, fill=self.palette["panel"], outline="", tags="glass")
        self.create_line(
            self.radius,
            4,
            width - self.radius - 2,
            4,
            fill=blend_hex(self.palette["panel"], "#ffffff", 0.16 + self.glow * 0.08),
            width=1,
            tags="glass",
        )
        self.tag_lower("glass")

    def pulse(self) -> None:
        self._pulse_serial += 1
        serial = self._pulse_serial
        frame = 0

        def step() -> None:
            nonlocal frame
            if serial != self._pulse_serial or not self.winfo_exists():
                return
            frame += 1
            phase = frame / 18
            self.glow = (1 - abs(phase * 2 - 1)) * 0.8 if phase <= 1 else 0.0
            self._draw()
            if frame < 18:
                self.after(18, step)
            else:
                self.glow = 0.0
                self._draw()

        step()


class GlassTabView(tk.Frame):
    """Rounded pill navigation with horizontally animated page transitions."""

    def __init__(self, parent, *, palette=None, **kwargs) -> None:
        self.palette = {**DEFAULT_PALETTE, **(palette or {})}
        super().__init__(parent, background=self.palette["bg"], borderwidth=0, **kwargs)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.nav = tk.Frame(self, background=self.palette["bg"], borderwidth=0)
        self.nav.grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.page_host = tk.Frame(self, background=self.palette["bg"], borderwidth=0)
        self.page_host.grid(row=1, column=0, sticky="nsew")
        self.pages: list[tk.Widget] = []
        self.buttons: list[GlassButton] = []
        self.current_index = -1
        self._animation_id: str | None = None

    def add(self, page, *, text: str) -> None:
        index = len(self.pages)
        self.pages.append(page)
        page.place_forget()
        button = GlassButton(
            self.nav,
            text=text.strip(),
            style="Tab.TButton",
            command=lambda value=index: self.select(value),
            palette=self.palette,
            backdrop=self.palette["bg"],
        )
        button.pack(side="left", padx=(0, 7))
        self.buttons.append(button)
        if self.current_index < 0:
            self.current_index = 0
            page.place(x=0, y=0, relwidth=1, relheight=1)
            button.set_selected(True)

    def select(self, tab=None):
        if tab is None:
            return self.pages[self.current_index] if self.current_index >= 0 else None
        index = self.pages.index(tab) if tab in self.pages else int(tab)
        if index == self.current_index or not 0 <= index < len(self.pages):
            return None
        if self._animation_id:
            try:
                self.after_cancel(self._animation_id)
            except tk.TclError:
                pass
            self._animation_id = None
        previous_index = self.current_index
        previous = self.pages[previous_index]
        upcoming = self.pages[index]
        direction = 1 if index > previous_index else -1
        width = max(640, self.page_host.winfo_width())
        upcoming.place(x=direction * width, y=0, relwidth=1, relheight=1)
        upcoming.lift()
        self.current_index = index
        for button_index, button in enumerate(self.buttons):
            button.set_selected(button_index == index)
        started = self.winfo_toplevel().tk.call("clock", "milliseconds")
        duration = 235.0

        def step() -> None:
            now = self.winfo_toplevel().tk.call("clock", "milliseconds")
            progress = clamp((float(now) - float(started)) / duration)
            eased = 1 - (1 - progress) ** 3
            upcoming.place_configure(x=round(direction * width * (1 - eased)))
            previous.place_configure(x=round(-direction * width * 0.16 * eased))
            if progress < 1:
                self._animation_id = self.after(16, step)
            else:
                previous.place_forget()
                upcoming.place_configure(x=0)
                self._animation_id = None
                self.event_generate("<<NotebookTabChanged>>")

        step()
        return None
