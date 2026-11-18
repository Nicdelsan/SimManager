"""Configuration of the display plugin (`plugins.display` in config.yaml)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DisplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_image_mb: float = Field(20.0, gt=0, le=1024)
    """Images above this are listed but not copied; the UI offers "download anyway"."""
    max_images_per_execution: int = Field(20, ge=1, le=500)
    """Newest images copied per execution; older new ones are only listed."""
    timeout_s: int = Field(300, ge=10, le=86_400)
    """The script is killed (GNU `timeout`) after this many seconds."""
    max_parallel_per_cluster: int = Field(1, ge=1, le=8)
    """Scripts running at the same time on one cluster (each holds one SSH channel)."""
    min_interval_s: int = Field(60, ge=10)
    """Shortest auto-refresh interval accepted."""
    schedule_tick_s: float = Field(30.0, gt=0, le=3600)
    """How often due auto-refreshes are checked."""
