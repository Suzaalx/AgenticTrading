"""Memory tab scaffold."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static

from sentinel.memory.recall import forget_lesson
from sentinel.store.db import connect, run_migrations
from sentinel.tui.widgets.common import fetch_rows


class MemoryScreen(Widget):
    """Lesson-memory browser with FTS search, detail, and forget."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("x", "forget_selected", "Forget lesson")]

    def __init__(self) -> None:
        super().__init__(id="memory-screen")
        self._lesson_ids: list[str] = []
        self._query = ""

    def compose(self) -> ComposeResult:
        with Vertical(classes="screen-body"):
            yield Input(placeholder="Search lessons (FTS, Enter to apply)", id="memory-search")
            with Horizontal():
                yield DataTable(id="memory-lessons", classes="panel")
                yield Static(id="memory-detail", classes="panel")

    def on_mount(self) -> None:
        table = self.query_one("#memory-lessons", DataTable)
        table.cursor_type = "row"
        table.add_columns("GRADE", "SYMBOL", "TAGS", "LESSON", "CREATED")
        self.hydrate()

    def hydrate(self, query: str | None = None) -> None:
        self._query = self._query if query is None else query.strip()
        if self._query:
            rows = fetch_rows(
                "SELECT lesson_id, symbol, setup_tags, lesson, grade, created_at "
                "FROM lessons WHERE lessons MATCH ? ORDER BY rank LIMIT 50",
                (self._query,),
            )
        else:
            rows = fetch_rows(
                "SELECT lesson_id, symbol, setup_tags, lesson, grade, created_at "
                "FROM lessons ORDER BY created_at DESC LIMIT 50"
            )
        table = self.query_one("#memory-lessons", DataTable)
        table.clear()
        self._lesson_ids = []
        for row in rows:
            self._lesson_ids.append(str(row["lesson_id"]))
            table.add_row(
                self._grade_markup(str(row["grade"] or "—")),
                row["symbol"] or "—",
                row["setup_tags"] or "—",
                row["lesson"] or "—",
                str(row["created_at"] or "")[:16],
                key=row["lesson_id"],
            )
        self._render_detail(self._lesson_ids[0] if self._lesson_ids else None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "memory-search":
            self.hydrate(event.value)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "memory-lessons":
            self._render_detail(self._selected_lesson_id())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "memory-lessons":
            self._render_detail(self._selected_lesson_id())

    def action_forget_selected(self) -> None:
        lesson_id = self._selected_lesson_id()
        if lesson_id is None:
            self.notify("No lesson selected", severity="warning")
            return
        with connect() as conn:
            run_migrations(conn)
            removed = forget_lesson(conn, lesson_id)
        self.notify("Lesson forgotten" if removed else "Lesson not found")
        self.hydrate(self._query)

    def _selected_lesson_id(self) -> str | None:
        if not self._lesson_ids:
            return None
        table = self.query_one("#memory-lessons", DataTable)
        index = max(0, min(table.cursor_row, len(self._lesson_ids) - 1))
        return self._lesson_ids[index]

    def _render_detail(self, lesson_id: str | None) -> None:
        if lesson_id is None:
            self.query_one("#memory-detail", Static).update("Lesson detail\nNo lessons found.")
            return
        rows = fetch_rows(
            "SELECT lesson_id, symbol, setup_tags, what_happened, lesson, grade, created_at "
            "FROM lessons WHERE lesson_id = ?",
            (lesson_id,),
        )
        if not rows:
            self.query_one("#memory-detail", Static).update(f"Lesson detail\nMissing {lesson_id}")
            return
        row = rows[0]
        self.query_one("#memory-detail", Static).update(
            "Lesson detail\n"
            f"ID: {row['lesson_id']}\n"
            f"Symbol: {row['symbol']}\n"
            f"Grade: {row['grade']}\n"
            f"Tags: {row['setup_tags']}\n"
            f"Created: {row['created_at']}\n\n"
            f"What happened:\n{row['what_happened']}\n\n"
            f"Lesson:\n{row['lesson']}\n\n"
            "Press x to forget this lesson."
        )

    @staticmethod
    def _grade_markup(grade: str) -> str:
        colors = {
            "good_call": "green",
            "bad_call": "red",
            "lucky": "yellow",
            "unlucky": "magenta",
        }
        color = colors.get(grade, "white")
        return f"[{color}]{grade}[/{color}]"
