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
    archival_deadline: dt.date | dt.datetime | None = notion("Archival Deadline", "date", id="T%5BAe")
    assistant_editor: tuple[str, ...] = notion("Assistant Editor", "relation", id="fw%3CT", default=())
    cameras: tuple[str, ...] = notion("Cameras", "relation", id="oI%5CE", default=())
    captions_translator: tuple[str, ...] = notion("Captions Translator", "relation", id="s_%3BO", default=())
    capture_resolutions: tuple[str, ...] = notion("Capture Resolutions", "multi_select", id="ErLd", default=())
    cinematographer: tuple[str, ...] = notion("Cinematographer", "relation", id="%5DB%5Dy", default=())
    colorist: tuple[str, ...] = notion("Colorist", "relation", id="Ol%7BI", default=())
    coordinator: dict[str, Any] | None = notion("Coordinator", "formula", id="fSi%5B")
    created_time: dt.datetime | None = notion("Created time", "created_time", id="%3EYfT")
    da_vinci_resolve_version: str | None = notion("DaVinci Resolve Version", "select", id="%7B%7BTv")
    date: dt.date | dt.datetime | None = notion("Date", "date", id="gztA")
    deadline_display: dict[str, Any] | None = notion("Deadline Display", "formula", id="KsX%60")
    delegate_producer: tuple[str, ...] = notion("Delegate Producer", "relation", id="eANv", default=())
    deletion_date: dt.date | dt.datetime | None = notion("Deletion Date", "date", id="nT%7Dh")
    delivery_format: tuple[str, ...] = notion("Delivery Format", "multi_select", id="U%5Ct%3A", default=())
    director: tuple[str, ...] = notion("Director", "relation", id="%60i%7DD", default=())
    editors: tuple[str, ...] = notion("Editors", "relation", id="%60u%5BK", default=())
    eps: float | int | None = notion("Eps", "number", id="%3AdT%5D")
    facilities_inclusions: tuple[str, ...] = notion("Facilities Inclusions", "multi_select", id="EEek", default=())
    facilities_inclusions_1: tuple[str, ...] = notion("Facilities Inclusions (1)", "multi_select", id="I%7C%7Du", default=())
    has_post_supe: str | None = notion("Has Post Supe?", "select", id="Qoay")
    health: str | None = notion("Health", "select", id="KRb%3F")
    inclusions: tuple[str, ...] = notion("Inclusions", "multi_select", id="%3D%3Dyq", default=())
    label_ae_1: dict[str, Any] | None = notion("Label AE (1)", "formula", id="nWNj")
    label_editor: dict[str, Any] | None = notion("Label Editor", "formula", id="SJ%7Bo")
    label_oe: dict[str, Any] | None = notion("Label OE", "formula", id="%5DSB%40")
    label_pm_notes: dict[str, Any] | None = notion("Label PM Notes", "formula", id="IJgM")
    label_sound: dict[str, Any] | None = notion("Label Sound", "formula", id="jf_Y")
    label_supe: dict[str, Any] | None = notion("Label Supe", "formula", id="wFOS")
    live_sound_recordist: tuple[str, ...] = notion("Live Sound Recordist", "relation", id="U%7B%5DH", default=())
    main_t6_services: tuple[str, ...] = notion("Main T6 Services ", "multi_select", id="LFMd", default=())
    musical_scorer: tuple[str, ...] = notion("Musical Scorer", "relation", id="qV%7Ct", default=())
    nle: tuple[str, ...] = notion("NLE", "multi_select", id="%3FC%3AV", default=())
    name: str | None = notion("Name", "title", id="title")
    online_editor: tuple[str, ...] = notion("Online Editor", "relation", id="osYe", default=())
    pm_notes: str | None = notion("PM Notes", "rich_text", id="%3FdP%3D")
    playdate: dt.date | dt.datetime | None = notion("Playdate", "date", id="BpTz")
    prod_coor_supe: tuple[str, ...] = notion("Prod Coor/Supe", "relation", id="fcnr", default=())
    project_code: str | None = notion("Project Code", "select", id="%3CmAp")
    project_health: str | None = notion("Project Health", "select", id="%5CTBO")
    project_type: str | None = notion("Project Type", "select", id="seqq")
    promo_inclusions: tuple[str, ...] = notion("Promo Inclusions", "multi_select", id="SfSI", default=())
    related_to_meeting_notes_project: tuple[str, ...] = notion("Related to Meeting Notes (Project)", "relation", id="IN%3Ag", default=())
    separator_1: dict[str, Any] | None = notion("Separator 1", "formula", id="%3D%5CNp")
    separator_2: dict[str, Any] | None = notion("Separator 2", "formula", id="E%3B%7Bm")
    sound_mixer: tuple[str, ...] = notion("Sound Mixer", "relation", id="alOI", default=())
    sound_tech: tuple[str, ...] = notion("Sound Tech", "relation", id="BdAq", default=())
    stage: str | None = notion("Stage", "select", id="%3B%60%60B")
    status: str | None = notion("Status", "select", id="%3DR%7DW")
    status_for_editors: str | None = notion("Status for Editors", "select", id="tU%5E_")
    studio: tuple[str, ...] = notion("Studio", "relation", id="JfO%3A", default=())
    submission_deadline: dt.date | dt.datetime | None = notion("Submission Deadline", "date", id="F%3AWf")
    t6_post_coordinator: tuple[str, ...] = notion("T6 Post Coordinator", "relation", id="_Kro", default=())
    trt: str | None = notion("TRT", "select", id="%3F%3BNg")
    tech_notes: str | None = notion("Tech Notes", "rich_text", id="qrKZ")
    vfx_editor: tuple[str, ...] = notion("VFX Editor", "relation", id="yllN", default=())
    vfx_supe: tuple[str, ...] = notion("VFX Supe", "relation", id="%3F%3BG%3E", default=())
    glowing_star_rocket: bool | None = notion("\U0001f31f\U0001f680", "checkbox", id="V%5DWb")
    wiki: tuple[str, ...] = notion("\U0001f4d4 Wiki", "relation", id="hdu%5C", default=())
    deliverables: tuple[str, ...] = notion("\U0001f4e6 Deliverables", "relation", id="%7CV_h", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactsRow(NotionModel):
    """Notion database Contacts."""

    DATABASE: ClassVar[str] = "contacts"
    aliases: str | None = notion("Aliases", "rich_text", id="%5DFlK")
    assistant_editor: tuple[str, ...] = notion("Assistant Editor", "relation", id="Ojq~", default=())
    contact_number: str | None = notion("Contact Number", "phone_number", id="LJr%3D")
    coor_j_os: tuple[str, ...] = notion("Coor JOs", "relation", id="lCUc", default=())
    dop_projects: tuple[str, ...] = notion("DOP Projects", "relation", id="yHG%40", default=())
    dp_projects: tuple[str, ...] = notion("DP Projects", "relation", id="OxbW", default=())
    director_projects: tuple[str, ...] = notion("Director Projects", "relation", id="Az%3Cn", default=())
    editor_projects: tuple[str, ...] = notion("Editor Projects", "relation", id="%3B%5D%5C%5C", default=())
    email: str | None = notion("Email", "email", id="%7B%3B%3A%3A")
    end_of_shift: str | None = notion("End of Shift", "select", id="P%5Bk%3E")
    name: str | None = notion("Name", "title", id="title")
    note_to_driver: str | None = notion("Note to Driver", "rich_text", id="tM%5Cs")
    os_format: str | None = notion("OS Format", "select", id="%3B%5C%5EK")
    position: tuple[str, ...] = notion("Position", "multi_select", id="zhcP", default=())
    post_supe_projects: tuple[str, ...] = notion("Post Supe Projects", "relation", id="XEV~", default=())
    projects_as_live_sound: tuple[str, ...] = notion("Projects as Live Sound", "relation", id="ch%5Ck", default=())
    projects_as_vfx_editor: tuple[str, ...] = notion("Projects as VFX Editor", "relation", id="QXaD", default=())
    related_to_job_orders_assigned: tuple[str, ...] = notion("Related to Job Orders (Assigned)", "relation", id="%3Ax%5Dg", default=())
    related_to_tasks_freelancer: tuple[str, ...] = notion("Related to Tasks (Freelancer)", "relation", id="ibTG", default=())
    software: tuple[str, ...] = notion("Software", "multi_select", id="F_%3Fl", default=())
    swing_drives_in_possession: tuple[str, ...] = notion("Swing Drives in Possession", "relation", id="nhqJ", default=())
    telegram_handle: str | None = notion("TelegramHandle", "rich_text", id="GvDU")
    telegram_user_id: str | None = notion("TelegramUserID", "rich_text", id="%5Cz%7Cf")
    wiki: tuple[str, ...] = notion("\U0001f4d4 Wiki", "relation", id="naL%5B", default=())
    coordinator_deliverables: tuple[str, ...] = notion("\U0001f4e6 Coordinator Deliverables", "relation", id="gGT%3F", default=())
    editor_deliverables: tuple[str, ...] = notion("\U0001f4e6 Editor Deliverables", "relation", id="%7DFjE", default=())
    projects: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Projects", "relation", id="SLP%60", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class StudiosRow(NotionModel):
    """Notion database Studios."""

    DATABASE: ClassVar[str] = "studios"
    aliases: tuple[str, ...] = notion("Aliases", "multi_select", id="u_%3BR", default=())
    average_redelivery_velocity: str | None = notion("Average Redelivery Velocity", "rich_text", id="Rpim")
    name: str | None = notion("Name", "title", id="title")
    ocp_intake_id: str | None = notion("OCP Intake ID", "rich_text", id="Sx%3DS")
    ocp_intake_source: str | None = notion("OCP Intake Source", "rich_text", id="%5CGvP")
    project_delay_from_past_24_months_expressed_as: str | None = notion("Project Delay from Past 24 Months expressed as %", "rich_text", id="c%7DAO")
    rolling_average_on_time_rate_of_24_months: str | None = notion("Rolling Average On Time Rate of 24 months", "rich_text", id="JeCR")
    rolling_average_redelivery_rate_of_24_months: tuple[str, ...] = notion("Rolling Average Redelivery Rate of 24 months", "multi_select", id="FipL", default=())
    projects: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Projects", "relation", id="%60njG", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class JobOrdersRow(NotionModel):
    """Notion database Job Orders."""

    DATABASE: ClassVar[str] = "job_orders"
    assigned: tuple[str, ...] = notion("Assigned", "relation", id="DIi%3C", default=())
    blocker: str | None = notion("Blocker ", "rich_text", id="VMu%3C")
    date: dt.date | dt.datetime | None = notion("Date", "date", id="Kx%5EM")
    deadline: dt.date | dt.datetime | None = notion("Deadline", "date", id="p~L%3F")
    deliverable: tuple[str, ...] = notion("Deliverable", "relation", id="pEwS", default=())
    department: str | None = notion("Department", "select", id="xHPF")
    health: dict[str, Any] | None = notion("Health", "formula", id="%3DY%5Bq")
    jo_notes: str | None = notion("JO Notes", "rich_text", id="zfMz")
    jo_number: str | None = notion("JO Number", "rich_text", id="%60%5EG_")
    link_settings: str | None = notion("Link Settings", "rich_text", id="_TkP")
    mode_of_transfer: str | None = notion("Mode of Transfer", "rich_text", id="Iy%3EZ")
    name: str | None = notion("Name", "title", id="title")
    pending: str | None = notion("Pending", "rich_text", id="TvFU")
    place: dict[str, Any] | None = notion("Place", "place", id="%3FfQ%5B")
    platform: str | None = notion("Platform", "rich_text", id="uPhA")
    projects: tuple[str, ...] = notion("Projects", "relation", id="EKDD", default=())
    purpose: str | None = notion("Purpose", "rich_text", id="Wc%3Ak")
    source_source_drive: str | None = notion("Source / Source Drive", "rich_text", id="%7DZ%5C%5E")
    status: str | None = notion("Status", "status", id="v%3Fln")
    subtitle: str | None = notion("Subtitle", "rich_text", id="%7Bb%60%40")
    task: str | None = notion("Task", "rich_text", id="%60qkx")
    telegram_key: str | None = notion("Telegram Key", "rich_text", id="DXpp")
    title: str | None = notion("Title", "rich_text", id="%5E%60%7BD")
    watchdog_notes: str | None = notion("Watchdog Notes", "rich_text", id="pUEd")
    watermark: str | None = notion("Watermark", "rich_text", id="X%5DN%3A")
    cc: tuple[str, ...] = notion("cc", "relation", id="%5BY%7D%7D", default=())
    coordinator: tuple[str, ...] = notion("\U0001f5c2\U0000fe0f Coordinator", "relation", id="V%3B%3B%7B", default=())
    t6_id: str | None = notion("T6 ID", "rich_text")
    t6_notes: str | None = notion("T6 Notes", "rich_text")


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliverablesRow(NotionModel):
    """Notion database Deliverables."""

    DATABASE: ClassVar[str] = "deliverables"
    class_: str | None = notion("Class", "select", id="~Bbg")
    created_time: dt.datetime | None = notion("Created time", "created_time", id="Lvu%3E")
    deadline: dt.date | dt.datetime | None = notion("Deadline", "date", id="zdNp")
    deliverable_class: str | None = notion("Deliverable Class", "select", id="Zyut")
    job_orders: tuple[str, ...] = notion("Job Orders", "relation", id="EHOB", default=())
    last_edited_time: dt.datetime | None = notion("Last edited time", "last_edited_time", id="%3DHCS")
    name: str | None = notion("Name", "title", id="title")
    notes: str | None = notion("Notes", "rich_text", id="%3DNVF")
    ocp_intake_id: str | None = notion("OCP Intake ID", "rich_text", id="%7CV%5Eb")
    ocp_intake_source: str | None = notion("OCP Intake Source", "rich_text", id="%3Bb~w")
    playdate: dt.date | dt.datetime | None = notion("Playdate", "date", id="%3FQ~%3D")
    status: str | None = notion("Status", "select", id="N%3B%5Bs")
    tasks: tuple[str, ...] = notion("Tasks", "relation", id="awZv", default=())
    coordinator: tuple[str, ...] = notion("\U0001f5c2\U0000fe0f Coordinator", "relation", id="C%7D%40%40", default=())
    editor: tuple[str, ...] = notion("\U0001f5c2\U0000fe0f Editor", "relation", id="%7DIx%5E", default=())
    projects: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Projects", "relation", id="CmVY", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class BaysRow(NotionModel):
    """Notion database Bays."""

    DATABASE: ClassVar[str] = "bays"
    adobe_email: str | None = notion("Adobe email", "rich_text", id="uL%5C%3A")
    avid_activation_id: str | None = notion("Avid Activation ID", "rich_text", id="f%60s_")
    avid_system_id: str | None = notion("Avid System ID", "rich_text", id="L%5E%3CF")
    device_uiid: str | None = notion("Device UIID", "rich_text", id="WeSw")
    done: bool | None = notion("Done", "checkbox", id="%60%5DH%3A")
    hardware_uuid: str | None = notion("Hardware UUID", "rich_text", id="ozz%5B")
    has_paragon: bool | None = notion("Has Paragon", "checkbox", id="cccC")
    hostname: str | None = notion("Hostname", "rich_text", id="%3CFGK")
    lan_ip_address: str | None = notion("LAN IP Address", "rich_text", id="%5BO%3A%5C")
    last_check_in: dt.date | dt.datetime | None = notion("Last Check-in", "date", id="%3DK%7CU")
    munki_client_version: str | None = notion("Munki Client Version", "rich_text", id="MMPF")
    munki_manifest: str | None = notion("Munki Manifest", "rich_text", id="ixPv")
    name: str | None = notion("Name", "title", id="title")
    notes: str | None = notion("Notes", "rich_text", id="o%40%3Ax")
    os_version: str | None = notion("OS Version", "rich_text", id="yuYX")
    paragon_license: str | None = notion("Paragon License", "rich_text", id="U%5C%5DS")
    paragon_ntfs_license: str | None = notion("Paragon NTFS License", "rich_text", id="Xgw%7C")
    related_to_tasks_property: tuple[str, ...] = notion("Related to Tasks (Property)", "relation", id="VIVf", default=())
    ring: str | None = notion("Ring", "select", id="dYyG")
    serial_number: str | None = notion("Serial Number", "rich_text", id="kvED")
    type: tuple[str, ...] = notion("Type", "multi_select", id="mFi%7B", default=())
    username: str | None = notion("Username", "rich_text", id="~%3DYa")


@dataclass(frozen=True, slots=True, kw_only=True)
class CamerasRow(NotionModel):
    """Notion database Cameras."""

    DATABASE: ClassVar[str] = "cameras"
    name: str | None = notion("Name", "title", id="title")
    related_to_projects_cameras: tuple[str, ...] = notion("Related to Projects (Cameras)", "relation", id="o%60xG", default=())
    tags: tuple[str, ...] = notion("Tags", "multi_select", id="lurO", default=())
    wiki: tuple[str, ...] = notion("\U0001f4d4 Wiki", "relation", id="Run%5C", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class SwingDrivesRow(NotionModel):
    """Notion database Swing Drives."""

    DATABASE: ClassVar[str] = "swing_drives"
    collaborator: tuple[str, ...] = notion("Collaborator", "relation", id="%5CJnv", default=())
    for_bawi: bool | None = notion("For Bawi", "checkbox", id="msGc")
    format: str | None = notion("Format ", "select", id="ecNh")
    hd_type: tuple[str, ...] = notion("HD Type", "multi_select", id="yQis", default=())
    in_use: bool | None = notion("In Use", "checkbox", id="e%3Fhq")
    last_moved: dt.date | dt.datetime | None = notion("Last Moved", "date", id="DasJ")
    name: str | None = notion("Name", "title", id="title")
    person_in_charge: tuple[str, ...] = notion("Person-In-Charge", "people", id="c%5CsT", default=())
    short_notes: str | None = notion("Short Notes", "rich_text", id="kb%3Bn")


@dataclass(frozen=True, slots=True, kw_only=True)
class EventsTasksRow(NotionModel):
    """Notion database Events & Tasks."""

    DATABASE: ClassVar[str] = "events_tasks"
    bay: tuple[str, ...] = notion("Bay", "relation", id="xslq", default=())
    created_time: dt.datetime | None = notion("Created time.   ", "created_time", id="Y%60u%5D")
    creator: tuple[str, ...] = notion("Creator", "relation", id="%3Fxy%5E", default=())
    deadline: dt.date | dt.datetime | None = notion("Deadline", "date", id="U%7DE%60")
    deliverable: tuple[str, ...] = notion("Deliverable", "relation", id="%5DrgB", default=())
    job_id: dict[str, Any] | None = notion("JOB ID", "unique_id", id="%3ErPu")
    last_updated: dt.datetime | None = notion("Last Updated", "last_edited_time", id="HG%3Ci")
    mode: str | None = notion("MODE", "select", id="RWEr")
    name: str | None = notion("Name", "title", id="title")
    pm_notes: str | None = notion("PM Notes", "rich_text", id="tMYF")
    project_code: dict[str, Any] | None = notion("Project Code", "rollup", id="EWsa")
    project_status: dict[str, Any] | None = notion("Project Status", "rollup", id="%7DHO%3D")
    project_type: dict[str, Any] | None = notion("Project Type", "rollup", id="DwDy")
    schedule: dt.date | dt.datetime | None = notion("Schedule", "date", id="YQF%3C")
    status: str | None = notion("Status", "status", id="XU%3ES")
    transfer_mode: tuple[str, ...] = notion("Transfer Mode", "multi_select", id="NaHw", default=())
    project: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Project", "relation", id="Y%3BOA", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class WikiRow(NotionModel):
    """Notion database Wiki."""

    DATABASE: ClassVar[str] = "wiki"
    created: dt.datetime | None = notion("Created", "created_time", id="G%5ChI")
    date: dt.date | dt.datetime | None = notion("Date", "date", id="ecaJ")
    definition: str | None = notion("Definition", "rich_text", id="z%5BSe")
    last_edited: dt.datetime | None = notion("Last Edited", "last_edited_time", id="t%60%3CW")
    name: str | None = notion("Name", "title", id="title")
    notes: str | None = notion("Notes", "rich_text", id="x%3Ddg")
    purpose: str | None = notion("Purpose", "rich_text", id="%3D_FQ")
    role: tuple[str, ...] = notion("Role", "multi_select", id="_%5CrX", default=())
    status: tuple[str, ...] = notion("Status", "multi_select", id="qfn_", default=())
    to_do: bool | None = notion("To Do", "checkbox", id="YzNc")
    topic: tuple[str, ...] = notion("Topic", "multi_select", id="%3Db%5Dp", default=())
    updated_by: dict[str, Any] | None = notion("Updated by", "last_edited_by", id="UYG%5C")
    cameras: tuple[str, ...] = notion("\U0001f3a5 Cameras", "relation", id="wr%5Cm", default=())
    contacts: tuple[str, ...] = notion("\U0001f5c2\U0000fe0f Contacts", "relation", id="K%5Byb", default=())
    projects: tuple[str, ...] = notion("\U0001f5fa\U0000fe0f Projects", "relation", id="tSf%5B", default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class MeetingNotesRow(NotionModel):
    """Notion database Meeting Notes."""

    DATABASE: ClassVar[str] = "meeting_notes"
    last_updated: dt.datetime | None = notion("Last Updated", "last_edited_time", id="qoRz")
    last_updated_by: dict[str, Any] | None = notion("Last Updated By", "last_edited_by", id="%5Es%3Be")
    meeting_date: dt.date | dt.datetime | None = notion("Meeting Date", "date", id="QF%5Bx")
    name: str | None = notion("Name", "title", id="title")
    project: tuple[str, ...] = notion("Project", "relation", id="%7BTVV", default=())
    ready_to_send: bool | None = notion("Ready to send", "checkbox", id="ZP%3AN")
    sent_at: dt.date | dt.datetime | None = notion("Sent at", "date", id="%5CtVO")
    team_attendees: tuple[str, ...] = notion("Team Attendees", "people", id="%3DKl%5C", default=())


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
