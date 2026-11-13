"""Notion models generated from `schema_snapshot.json`. Do not edit.

Run `uv run python scripts/generate_models.py` to rebuild this module after the
snapshot changes. Each field maps a Python name to one Notion property ID and name.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, ClassVar

from t6.notion.model import NotionModel, notion


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectsRow(NotionModel):
    """Notion database Projects."""

    DATABASE: ClassVar[str] = "projects"
    archival_deadline: dt.date | dt.datetime | None = notion("Archival Deadline", "date")
    assistant_editor: tuple[str, ...] = notion("Assistant Editor", "relation", default=())
    cameras: tuple[str, ...] = notion("Cameras", "relation", default=())
    captions_translator: tuple[str, ...] = notion("Captions Translator", "relation", default=())
    capture_resolutions: tuple[str, ...] = notion("Capture Resolutions", "multi_select", default=())
    cinematographer: tuple[str, ...] = notion("Cinematographer", "relation", default=())
    colorist: tuple[str, ...] = notion("Colorist", "relation", default=())
    coordinator: dict[str, Any] | None = notion("Coordinator", "formula")
    created_time: dt.datetime | None = notion("Created time", "created_time")
    da_vinci_resolve_version: str | None = notion("DaVinci Resolve Version", "select")
    date: dt.date | dt.datetime | None = notion("Date", "date")
    deadline_display: dict[str, Any] | None = notion("Deadline Display", "formula")
    delegate_producer: tuple[str, ...] = notion("Delegate Producer", "relation", default=())
    deletion_date: dt.date | dt.datetime | None = notion("Deletion Date", "date")
    delivery_format: tuple[str, ...] = notion("Delivery Format", "multi_select", default=())
    director: tuple[str, ...] = notion("Director", "relation", default=())
    editors: tuple[str, ...] = notion("Editors", "relation", default=())
    eps: float | int | None = notion("Eps", "number")
    facilities_inclusions: tuple[str, ...] = notion("Facilities Inclusions", "multi_select", default=())
    facilities_inclusions_1: tuple[str, ...] = notion("Facilities Inclusions (1)", "multi_select", default=())
    has_post_supe: str | None = notion("Has Post Supe?", "select")
    health: str | None = notion("Health", "select")
    inclusions: tuple[str, ...] = notion("Inclusions", "multi_select", default=())
    label_ae_1: dict[str, Any] | None = notion("Label AE (1)", "formula")
    label_editor: dict[str, Any] | None = notion("Label Editor", "formula")
    label_oe: dict[str, Any] | None = notion("Label OE", "formula")
    label_pm_notes: dict[str, Any] | None = notion("Label PM Notes", "formula")
    label_sound: dict[str, Any] | None = notion("Label Sound", "formula")
    label_supe: dict[str, Any] | None = notion("Label Supe", "formula")
    live_sound_recordist: tuple[str, ...] = notion("Live Sound Recordist", "relation", default=())
    main_t6_services: tuple[str, ...] = notion("Main T6 Services ", "multi_select", default=())
    musical_scorer: tuple[str, ...] = notion("Musical Scorer", "relation", default=())
    nle: tuple[str, ...] = notion("NLE", "multi_select", default=())
    name: str | None = notion("Name", "title")
    online_editor: tuple[str, ...] = notion("Online Editor", "relation", default=())
    pm_notes: str | None = notion("PM Notes", "rich_text")
    playdate: dt.date | dt.datetime | None = notion("Playdate", "date")
    prod_coor_supe: tuple[str, ...] = notion("Prod Coor/Supe", "relation", default=())
    project_code: str | None = notion("Project Code", "select")
    project_health: str | None = notion("Project Health", "select")
    project_type: str | None = notion("Project Type", "select")
    promo_inclusions: tuple[str, ...] = notion("Promo Inclusions", "multi_select", default=())
    related_to_meeting_notes_project: tuple[str, ...] = notion("Related to Meeting Notes (Project)", "relation", default=())
    separator_1: dict[str, Any] | None = notion("Separator 1", "formula")
    separator_2: dict[str, Any] | None = notion("Separator 2", "formula")
    sound_mixer: tuple[str, ...] = notion("Sound Mixer", "relation", default=())
    sound_tech: tuple[str, ...] = notion("Sound Tech", "relation", default=())
    stage: str | None = notion("Stage", "select")
    status: str | None = notion("Status", "select")
    status_for_editors: str | None = notion("Status for Editors", "select")
    studio: tuple[str, ...] = notion("Studio", "relation", default=())
    submission_deadline: dt.date | dt.datetime | None = notion("Submission Deadline", "date")
    t6_post_coordinator: tuple[str, ...] = notion("T6 Post Coordinator", "relation", default=())
    trt: str | None = notion("TRT", "select")
    tech_notes: str | None = notion("Tech Notes", "rich_text")
    vfx_editor: tuple[str, ...] = notion("VFX Editor", "relation", default=())
    vfx_supe: tuple[str, ...] = notion("VFX Supe", "relation", default=())
    glowing_star_rocket: bool | None = notion("\U0001f31f\U0001f680", "checkbox")
    wiki: tuple[str, ...] = notion("\U0001f4d4 Wiki", "relation", default=())
    deliverables: tuple[str, ...] = notion("\U0001f4e6 Deliverables", "relation", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactsRow(NotionModel):
    """Notion database Contacts."""

    DATABASE: ClassVar[str] = "contacts"
    aliases: str | None = notion("Aliases", "rich_text")
    assistant_editor: tuple[str, ...] = notion("Assistant Editor", "relation", default=())
    contact_number: str | None = notion("Contact Number", "phone_number")
    coor_j_os: tuple[str, ...] = notion("Coor JOs", "relation", default=())
    dop_projects: tuple[str, ...] = notion("DOP Projects", "relation", default=())
    dp_projects: tuple[str, ...] = notion("DP Projects", "relation", default=())
    director_projects: tuple[str, ...] = notion("Director Projects", "relation", default=())
    editor_projects: tuple[str, ...] = notion("Editor Projects", "relation", default=())
    email: str | None = notion("Email", "email")
    end_of_shift: str | None = notion("End of Shift", "select")
    name: str | None = notion("Name", "title")
    note_to_driver: str | None = notion("Note to Driver", "rich_text")
    os_format: str | None = notion("OS Format", "select")
    position: tuple[str, ...] = notion("Position", "multi_select", default=())
    post_supe_projects: tuple[str, ...] = notion("Post Supe Projects", "relation", default=())
    projects_as_live_sound: tuple[str, ...] = notion("Projects as Live Sound", "relation", default=())
    projects_as_vfx_editor: tuple[str, ...] = notion("Projects as VFX Editor", "relation", default=())
    related_to_job_orders_assigned: tuple[str, ...] = notion("Related to Job Orders (Assigned)", "relation", default=())
    related_to_tasks_freelancer: tuple[str, ...] = notion("Related to Tasks (Freelancer)", "relation", default=())
    software: tuple[str, ...] = notion("Software", "multi_select", default=())
    swing_drives_in_possession: tuple[str, ...] = notion("Swing Drives in Possession", "relation", default=())
    telegram_handle: str | None = notion("TelegramHandle", "rich_text")
    telegram_user_id: str | None = notion("TelegramUserID", "rich_text")
    wiki: tuple[str, ...] = notion("\U0001f4d4 Wiki", "relation", default=())
    coordinator_deliverables: tuple[str, ...] = notion("\U0001f4e6 Coordinator Deliverables", "relation", default=())
    editor_deliverables: tuple[str, ...] = notion("\U0001f4e6 Editor Deliverables", "relation", default=())
    projects: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Projects", "relation", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class StudiosRow(NotionModel):
    """Notion database Studios."""

    DATABASE: ClassVar[str] = "studios"
    aliases: tuple[str, ...] = notion("Aliases", "multi_select", default=())
    average_redelivery_velocity: str | None = notion("Average Redelivery Velocity", "rich_text")
    name: str | None = notion("Name", "title")
    ocp_intake_id: str | None = notion("OCP Intake ID", "rich_text")
    ocp_intake_source: str | None = notion("OCP Intake Source", "rich_text")
    project_delay_from_past_24_months_expressed_as: str | None = notion("Project Delay from Past 24 Months expressed as %", "rich_text")
    rolling_average_on_time_rate_of_24_months: str | None = notion("Rolling Average On Time Rate of 24 months", "rich_text")
    rolling_average_redelivery_rate_of_24_months: tuple[str, ...] = notion("Rolling Average Redelivery Rate of 24 months", "multi_select", default=())
    projects: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Projects", "relation", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class JobOrdersRow(NotionModel):
    """Notion database Job Orders."""

    DATABASE: ClassVar[str] = "job_orders"
    assigned: tuple[str, ...] = notion("Assigned", "relation", default=())
    blocker: str | None = notion("Blocker ", "rich_text")
    date: dt.date | dt.datetime | None = notion("Date", "date")
    deadline: dt.date | dt.datetime | None = notion("Deadline", "date")
    deliverable: tuple[str, ...] = notion("Deliverable", "relation", default=())
    department: str | None = notion("Department", "select")
    health: dict[str, Any] | None = notion("Health", "formula")
    jo_notes: str | None = notion("JO Notes", "rich_text")
    jo_number: str | None = notion("JO Number", "rich_text")
    link_settings: str | None = notion("Link Settings", "rich_text")
    mode_of_transfer: str | None = notion("Mode of Transfer", "rich_text")
    name: str | None = notion("Name", "title")
    pending: str | None = notion("Pending", "rich_text")
    place: dict[str, Any] | None = notion("Place", "place")
    platform: str | None = notion("Platform", "rich_text")
    projects: tuple[str, ...] = notion("Projects", "relation", default=())
    purpose: str | None = notion("Purpose", "rich_text")
    source_source_drive: str | None = notion("Source / Source Drive", "rich_text")
    status: str | None = notion("Status", "status")
    subtitle: str | None = notion("Subtitle", "rich_text")
    task: str | None = notion("Task", "rich_text")
    telegram_key: str | None = notion("Telegram Key", "rich_text")
    title: str | None = notion("Title", "rich_text")
    watchdog_notes: str | None = notion("Watchdog Notes", "rich_text")
    watermark: str | None = notion("Watermark", "rich_text")
    cc: tuple[str, ...] = notion("cc", "relation", default=())
    coordinator: tuple[str, ...] = notion("\U0001f5c2\U0000fe0f Coordinator", "relation", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliverablesRow(NotionModel):
    """Notion database Deliverables."""

    DATABASE: ClassVar[str] = "deliverables"
    class_: str | None = notion("Class", "select")
    created_time: dt.datetime | None = notion("Created time", "created_time")
    deadline: dt.date | dt.datetime | None = notion("Deadline", "date")
    deliverable_class: str | None = notion("Deliverable Class", "select")
    job_orders: tuple[str, ...] = notion("Job Orders", "relation", default=())
    last_edited_time: dt.datetime | None = notion("Last edited time", "last_edited_time")
    name: str | None = notion("Name", "title")
    notes: str | None = notion("Notes", "rich_text")
    ocp_intake_id: str | None = notion("OCP Intake ID", "rich_text")
    ocp_intake_source: str | None = notion("OCP Intake Source", "rich_text")
    playdate: dt.date | dt.datetime | None = notion("Playdate", "date")
    status: str | None = notion("Status", "select")
    tasks: tuple[str, ...] = notion("Tasks", "relation", default=())
    coordinator: tuple[str, ...] = notion("\U0001f5c2\U0000fe0f Coordinator", "relation", default=())
    editor: tuple[str, ...] = notion("\U0001f5c2\U0000fe0f Editor", "relation", default=())
    projects: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Projects", "relation", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class BaysRow(NotionModel):
    """Notion database Bays."""

    DATABASE: ClassVar[str] = "bays"
    adobe_email: str | None = notion("Adobe email", "rich_text")
    avid_activation_id: str | None = notion("Avid Activation ID", "rich_text")
    avid_system_id: str | None = notion("Avid System ID", "rich_text")
    device_uiid: str | None = notion("Device UIID", "rich_text")
    done: bool | None = notion("Done", "checkbox")
    hardware_uuid: str | None = notion("Hardware UUID", "rich_text")
    has_paragon: bool | None = notion("Has Paragon", "checkbox")
    hostname: str | None = notion("Hostname", "rich_text")
    lan_ip_address: str | None = notion("LAN IP Address", "rich_text")
    last_check_in: dt.date | dt.datetime | None = notion("Last Check-in", "date")
    munki_client_version: str | None = notion("Munki Client Version", "rich_text")
    munki_manifest: str | None = notion("Munki Manifest", "rich_text")
    name: str | None = notion("Name", "title")
    notes: str | None = notion("Notes", "rich_text")
    os_version: str | None = notion("OS Version", "rich_text")
    paragon_license: str | None = notion("Paragon License", "rich_text")
    paragon_ntfs_license: str | None = notion("Paragon NTFS License", "rich_text")
    related_to_tasks_property: tuple[str, ...] = notion("Related to Tasks (Property)", "relation", default=())
    ring: str | None = notion("Ring", "select")
    serial_number: str | None = notion("Serial Number", "rich_text")
    type: tuple[str, ...] = notion("Type", "multi_select", default=())
    username: str | None = notion("Username", "rich_text")


@dataclass(frozen=True, slots=True, kw_only=True)
class CamerasRow(NotionModel):
    """Notion database Cameras."""

    DATABASE: ClassVar[str] = "cameras"
    name: str | None = notion("Name", "title")
    related_to_projects_cameras: tuple[str, ...] = notion("Related to Projects (Cameras)", "relation", default=())
    tags: tuple[str, ...] = notion("Tags", "multi_select", default=())
    wiki: tuple[str, ...] = notion("\U0001f4d4 Wiki", "relation", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class SwingDrivesRow(NotionModel):
    """Notion database Swing Drives."""

    DATABASE: ClassVar[str] = "swing_drives"
    collaborator: tuple[str, ...] = notion("Collaborator", "relation", default=())
    for_bawi: bool | None = notion("For Bawi", "checkbox")
    format: str | None = notion("Format ", "select")
    hd_type: tuple[str, ...] = notion("HD Type", "multi_select", default=())
    in_use: bool | None = notion("In Use", "checkbox")
    last_moved: dt.date | dt.datetime | None = notion("Last Moved", "date")
    name: str | None = notion("Name", "title")
    person_in_charge: tuple[str, ...] = notion("Person-In-Charge", "people", default=())
    short_notes: str | None = notion("Short Notes", "rich_text")


@dataclass(frozen=True, slots=True, kw_only=True)
class EventsTasksRow(NotionModel):
    """Notion database Events & Tasks."""

    DATABASE: ClassVar[str] = "events_tasks"
    bay: tuple[str, ...] = notion("Bay", "relation", default=())
    created_time: dt.datetime | None = notion("Created time.   ", "created_time")
    creator: tuple[str, ...] = notion("Creator", "relation", default=())
    deadline: dt.date | dt.datetime | None = notion("Deadline", "date")
    deliverable: tuple[str, ...] = notion("Deliverable", "relation", default=())
    job_id: dict[str, Any] | None = notion("JOB ID", "unique_id")
    last_updated: dt.datetime | None = notion("Last Updated", "last_edited_time")
    mode: str | None = notion("MODE", "select")
    name: str | None = notion("Name", "title")
    pm_notes: str | None = notion("PM Notes", "rich_text")
    project_code: dict[str, Any] | None = notion("Project Code", "rollup")
    project_status: dict[str, Any] | None = notion("Project Status", "rollup")
    project_type: dict[str, Any] | None = notion("Project Type", "rollup")
    schedule: dt.date | dt.datetime | None = notion("Schedule", "date")
    status: str | None = notion("Status", "status")
    transfer_mode: tuple[str, ...] = notion("Transfer Mode", "multi_select", default=())
    project: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Project", "relation", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class WikiRow(NotionModel):
    """Notion database Wiki."""

    DATABASE: ClassVar[str] = "wiki"
    created: dt.datetime | None = notion("Created", "created_time")
    date: dt.date | dt.datetime | None = notion("Date", "date")
    definition: str | None = notion("Definition", "rich_text")
    last_edited: dt.datetime | None = notion("Last Edited", "last_edited_time")
    name: str | None = notion("Name", "title")
    notes: str | None = notion("Notes", "rich_text")
    purpose: str | None = notion("Purpose", "rich_text")
    role: tuple[str, ...] = notion("Role", "multi_select", default=())
    status: tuple[str, ...] = notion("Status", "multi_select", default=())
    to_do: bool | None = notion("To Do", "checkbox")
    topic: tuple[str, ...] = notion("Topic", "multi_select", default=())
    updated_by: dict[str, Any] | None = notion("Updated by", "last_edited_by")
    cameras: tuple[str, ...] = notion("\U0001f3a5 Cameras", "relation", default=())
    contacts: tuple[str, ...] = notion("\U0001f5c2\U0000fe0f Contacts", "relation", default=())
    projects: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Projects", "relation", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class MeetingNotesRow(NotionModel):
    """Notion database Meeting Notes."""

    DATABASE: ClassVar[str] = "meeting_notes"
    last_updated: dt.datetime | None = notion("Last Updated", "last_edited_time")
    last_updated_by: dict[str, Any] | None = notion("Last Updated By", "last_edited_by")
    meeting_date: dt.date | dt.datetime | None = notion("Meeting Date", "date")
    name: str | None = notion("Name", "title")
    project: tuple[str, ...] = notion("Project", "relation", default=())
    ready_to_send: bool | None = notion("Ready to send", "checkbox")
    sent_at: dt.date | dt.datetime | None = notion("Sent at", "date")
    team_attendees: tuple[str, ...] = notion("Team Attendees", "people", default=())


MODELS: dict[str, type[NotionModel]] = {
    "projects": ProjectsRow,
    "contacts": ContactsRow,
    "studios": StudiosRow,
    "job_orders": JobOrdersRow,
    "deliverables": DeliverablesRow,
    "bays": BaysRow,
    "cameras": CamerasRow,
    "swing_drives": SwingDrivesRow,
    "events_tasks": EventsTasksRow,
    "wiki": WikiRow,
    "meeting_notes": MeetingNotesRow,
}
"""Every generated model, by snapshot database key."""
