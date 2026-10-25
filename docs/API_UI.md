# Web API contract

Application JSON APIs are same-origin and authenticated except the public authentication status endpoint. Errors use `{error: string}` and non-2xx responses; attachment/export endpoints return the specified binary or text format. State and activity reads do not create activities. All POST requests require `Content-Type: application/json` and `X-DSH-Client: web`; a supplied Origin must match Host. Timestamp display uses Asia/Shanghai.

## Authentication

- `GET /api/auth/status` → `{configured,authenticated,setup_available,username?}`; username is returned only for an authenticated account session.
- `POST /api/auth/setup {username,password,remember}` creates the single account only when no account exists, from a real loopback connection with the exact local HTTP Origin. Username is 2–64 characters and password 10–256 characters. Forwarded requests cannot perform initial setup.
- `POST /api/login {username,password,remember}` authenticates the existing account. `remember:true` gives a 30-day persisted session; false gives a browser-session cookie with a server-side maximum of 12 hours. Valid sessions survive normal application restarts.
- `POST /api/auth/password {current_password,new_password,remember}` changes the password and revokes the other sessions. `POST /api/logout {}` revokes the current session.

Setup/login/password-change return `{ok:true}` and an HttpOnly, SameSite=Strict session cookie; HTTPS requests add Secure. Auth failures are generic and rate limited. Once an account is configured, the legacy long access token is not a login method. The pre-account migration fallback is not the normal UI workflow. Account files and session hashes are excluded from export and backup; see [ACCOUNTS.md](ACCOUNTS.md).

`GET /api/state?date=YYYY-MM-DD&q=keyword&journal_mode=day|all|undated&week_date=YYYY-MM-DD` returns the state below.

## Read responses

`state`: `{today,date,activities:[],recent_activities:[],routine_projects:[],chronicle:[],chronicle_settings:{},calendar_events:[],bookmarks:[],schedules:[],tasks:[],gaps:[],journal:[],profiles:[],growth:[],imports:[],weekly_review:{},health:{},budget:{}}`.

`activities` contains the requested business date. `recent_activities` contains at most 60 `CHAT` activities, ordered by recent business/creation date. `routine_projects` contains `{routine,name,count,activities:[{id,routine,business_date,status,ready_at}]}` for A1, A3 and A2; project dates are not truncated to the recent-chat limit. The UI must not mix those routines into recent conversations.

`chronicle` contains personal-history entries across dates, independently of transcript date/search filters. `calendar_events` covers events overlapping the selected date’s month. Ordinary `profiles` excludes the internal `chronicle` scope.

Activity: `{id,routine:A1|A2|A3|CHAT,business_date,status,session_id,version,created_at,ready_at,summary,notification_state,bookmarked}`.

Schedule: `{routine,time,weekdays:[0..6],paused_until,next_at}`; weekday is Python weekday, Monday=0.

Gap row: `{date,routine,status,activity_id}`. States include missing, before_start, paused, not_scheduled, skipped, awaiting_reply, partial, basic_complete; no absent-data deficit inference.

Transcript (`journal`) entry: `{id,role,body,received_at,activity_id,business_date,routine,source}`. Imported messages also carry their source identifiers and original/import timestamps. This remains the complete raw-message view at `#transcript`; it is not the personal-history timeline at `#journal`.

Task: `{id,goal,status,owner,run_at,interval_days,source_activity_id,acceptance,version,result,deliveries:[{version,body,created_at,status}],error}`.

`GET /api/activity?id=...`: `{activity,messages:[{id,role,body,received_at,submission_id,processing_state}],summary,corrections:[],attachments:[]}`. Summary can be object or text; do not send fabricated defaults. `GET /api/export` returns complete downloadable archive; `GET /api/export?format=markdown` returns the readable raw transcript (`diary.md`). `GET /api/export?format=chronicle` returns only recorded personal-history entries and their source references (`personal-history.md`), excluding candidates.

Chat Markdown rendering is a presentation step. API message bodies, persisted source text, drafts, raw transcripts and exports retain the original characters. The renderer supports headings, lists, blockquotes, tables, inline/fenced code and safe HTTP/HTTPS links; code blocks and wide tables scroll inside their own containers. Raw HTML and scripts remain inert text, and remote images are not automatically loaded. A rendered link is not an instruction to fetch, import or execute its destination.

## Actions

All below `POST /api/action {op,...fields}`:

- `ensure_activity`: `{routine,date}` → `{activity}` (existing object reused). CHAT provides main chat.
- `message`: `{activity_id,submission_id,body}` → `{receipt,activity_id,processing_state}`. Receipt must denote committed durable input, not mere accepted request. UI will NOT claim saved without truthy receipt. Same submission ID reused after uncertain transport result.
- `activity_control`: `{activity_id,action:continue|skip|end|snooze|bookmark,remind_at?,value?}`. Snooze explicit ISO timestamp; bookmark value boolean.
- `presented`: `{activity_id,visible:true,interaction_at}` only after visible DOM rendered and user interacted in recent five minutes. Server validates live connection/lease.
- `natural`: `{body}` → `{activity?,activities?,message?}`. Routes explicit history requests; otherwise clarifies, never fabricates dates.
- `schedule`: `{routine,time,weekdays,paused_until,source}`. Empty pause clears; source records exact explicit UI operation.
- `correction`: `{activity_id,fields:{field: value},source}`. Persistent correction overlays; empty fields no mutation.
- `task_create`: `{goal,owner,inputs,deliverable,acceptance,run_at,interval_days,source_activity_id}`; explicit submit is authorization within stated goal.
- `task_review`: `{task_id,version,action:accept|revise|cancel,input}`. Exact displayed version required; input original user wording.
- `task_run`: `{task_id}`.
- `import_preview` / `import_apply`: `{source:kimi|codex|chatgpt,kind:conversation|memory|instructions,scope,filename,content,selected_message_ids?,preview_token?}`. Preview returns `{counts:{total,new,duplicates,deleted},date_range,failures,warnings,missing_attachments,items,selected_message_ids,sample,preview_token,...}`. `items` lists all parsed source messages as `{message_id,conversation_id,title,role,body,original_at}`; counts and sample reflect the requested selection. Apply must reuse the same source bytes, selection and preview token. An empty selection is rejected; omitted selection retains the full-import API compatibility path. A file may contain multiple conversations. The UI supports selecting the batch with optional exclusions and requires a selection preview before apply; it does not require transferring or approving each conversation separately.
- `import_removal_plan`: `{batch_id}` previews dependent records; `import_remove`: `{batch_id,plan_token}` applies the reviewed scope.
- `profile_save`: `{id?,content,category,scope,source_ref,confirmed,valid_until?}`.
- `profile_disable`: `{id}`.
- `growth_save`: `{line:doctorate|startup|culture,kind:goal|capability|plan|action|outcome|review|revision,content,source_ref,parent_id?,review_date?,confirmed?}`.
- `deletion_plan`: `{message_id}` previews affected records. `delete_message`: `{message_id,scope:'raw_and_derived',source,plan_token}` applies the reviewed scope. The UI explicitly confirms deletion scope; a stale plan is rejected and backup retention limitations remain visible.
- `retry`: `{activity_id?}` retry failed processing, not raw input.
- `cleanup`: `{scope:'regenerable'}`.

Health is factual service diagnostics; no UI-created success values. Typical keys `model`, `model_error`, `notification`, `backup`, `pending`, `last_success`, `storage_bytes`, `version`, `dsh_url`, `observation`.

## Integration additions

- `GET /api/events` provides an SSE `hello` with `{lease}`. Foreground `presented` sends the lease and only acknowledges visibly rendered prompts while this connection is alive and there was recent interaction.
- State includes `bookmarks` across all dates and `weekly_review`. Optional `week_date` selects the natural week containing that Shanghai date for `weekly_review` only; UI sends today or today minus seven days for the current/previous complete week. It must not filter the global growth item list. Notification `accepted` displays “接口已接受” and is distinct from task acceptance. `growth` may be `{items: [...]}` across lines. `budget.usage` contains persistent settled/reserved rows by day and kind. UI sums only background usage.
- `GET /api/source?ref=` resolves message/import/profile/growth/task/activity/calendar references for the source viewer.
- `journal_mode=day|all|undated` explicitly chooses chronology scope; records are not silently cut at 200. Client pagination displays 50 raw records per page with full count. Unknown original dates remain unknown, with real import timestamps shown.
- `attachment_upload {activity_id,filename,content_base64,mime_type}` saves a file up to 10 MB. `GET /api/attachment?id=` returns its bytes.
- Import apply carries the exact previewed bytes and `preview_token`. Profile and growth edits carry `item_id`, `expected_version`, `source_refs`, and exact `user_input`; growth carries original `business_date`.
- One-off tasks use `interval_days:null`. Owners: `agent`, `user`, `共同`. Pausing includes the selected end date and resumes the next day.

## Native pending questions

`GET /api/activity` additionally returns `questions:[{callId,questions:[{id,question,options?:[string|{label,description?}]}],state}]`. Unanswered questions render above the ongoing conversation. Choosing a question associates `call_id` and `question_id` with the next durable `message` action. Choosing an option fills the composer only; no implicit send. The association can be cleared to send an ordinary follow-up. A pending submission persists its original question target and stable submission ID across network retries and page reloads.

Natural-language history commands carry a stable `submission_id` persisted in browser storage with the exact original body until a successful server response. Uncertain retries—including after a midnight date change—reuse the same ID; a different command cannot overwrite an unconfirmed operation.

Task creation now sends a `submission_id` generated once per open task form. Retrying an uncertain request keeps that identity and payload; a newly opened form receives a new identity. Backend deduplication rejects reuse with different content.

## Personal history

`GET /api/chronicle?date=YYYY-MM-DD&q=...` returns history entries; omit date for all dates, or use `date=unknown` for unknown original dates. `state.chronicle` is the unfiltered history list. Entries include `{id,kind,title,body,business_date,recorded_at,status:recorded|candidate,formation?,source_refs,source_ids?,activity_id,import_id?,correction_version?,links}`. `candidate` means pending source clarification or processing, displayed as “待整理”; it is not a request for personal approval. `formation` can distinguish `source_projection`, `agent_synthesis` and `manual_revision`. Calendar completion entries reference `calendar:<id>` and do not use the conversation-revision actions.

Under the user's standing authorization, sufficient user source material forms personal history automatically as third-person biographical narration. It uses the explicitly configured user title, or “用户” when none has been configured; it does not invent a name or honorific. Direct quotations preserve the original first person. Existing explicit titles and manually edited prose are preserved. Source projections remain available when model processing is unavailable. Automatic synthesis binds source context and its version, distinguishes facts, plans and personal judgments, and preserves unknown dates and unresolved contradictions. Assistant reference drafts alone are not user experiences. Automatic `recorded` status does not imply human confirmation: automatic synthesis records agent provenance and does not set `confirmed=true` on the user's behalf.

`chronicle_settings` is `{user_title:'用户',assistant_name:'起居',role:'个人史秉笔史官'}` when no user title has been configured. A previously saved explicit title, including “本人”, is not migrated to this fallback. Actions:

- `chronicle_settings {user_title}` saves 1–32 visible characters. It changes automatic narration, not raw messages or silently rewriting user-edited prose.
- `chronicle_revise {activity_id,body,source_ids,expected_version,excluded}` explicitly revises or excludes a local activity entry after automatic formation, or supplies a manual entry from its sources. Source IDs are raw message IDs without the `message:` prefix; the version is `correction_version` from the displayed entry. This action is not a prerequisite for automatic recording.
- `chronicle_import_revise {message_id,body,expected_version,excluded}` does the same for an imported user source, using `entry.import_id`; the first expected version is 0.

Exclusion retains the original conversation; source deletion invalidates dependent narrative rather than revealing an earlier version that relies on the removed source. Explicit user revisions and exclusions take precedence over subsequent automatic synthesis. Chronicle records remain in the dedicated `chronicle` scope and do not automatically become global profiles. A historical user-confirmed entry retains that provenance, without imposing a confirmation gate on future entries. Task delivery acceptance, full-project acceptance and marking calendar events complete still require the user's explicit action. See [CHRONICLE.md](CHRONICLE.md).

## Calendar

`GET /api/calendar?month=YYYY-MM` returns events overlapping that Shanghai month. `GET /api/source?ref=calendar:<id>` resolves a specific event; `#calendar/<id>` opens its date, including other months.

Event fields include `{id,title,start_at,end_at,all_day,location,notes,reminder_minutes,status:planned|completed|cancelled,version,notification_state,reminder_at,notification_receipt,created_at,updated_at,completed_at}`. Timestamps contain an explicit offset and are stored in UTC. The UI converts Shanghai `datetime-local` inputs to `+08:00` before submitting.

- `calendar_save {submission_id,title,start_at,end_at,all_day,location,notes,reminder_minutes}` creates a planned event. `submission_id` stays stable while retrying the same form after an uncertain receipt.
- `calendar_save {event_id,expected_version,...event_fields}` edits an existing planned event. End must be later than start. Completed/cancelled events cannot be overwritten as new plans.
- `calendar_status {event_id,expected_version,status:completed|cancelled}` requires the explicit user confirmation displayed by the UI. Time passing and notification acceptance never complete an event.

`reminder_minutes:null` disables reminders; integers 0–10080 opt in, provided the reminder time is still future. The notification body omits the private title, place and notes. Unknown delivery is not automatically retried, and restoring pending reminders requires explicit review rather than replay. These one-off events do not alter A1–A3 schedules. See [CALENDAR.md](CALENDAR.md).

## Materials guidance

`/guide/materials`, `/guide/material-template` and `/guide/material-example` serve the project's preparation instructions, optional short template and explicitly synthetic JSON example as plain text. `/guide/chatgpt-material-instructions`, `/guide/kimi-material-instructions` and `/guide/material-batch-example` additionally serve the complete platform prompts and synthetic batch example. These files contain no private source material. See the [GPT instructions](templates/chatgpt-material-instructions.md), [Kimi instructions](templates/kimi-material-instructions.md) and [batch format](templates/material-batch-format.md).

Each platform prepares files from only the history actually visible to it or supplied by the user, declaring uncovered scope. Source descriptions, genuine user quotes and assistant reference drafts stay distinct; no draft is relabeled as a user quote. The application previews the resulting files in batches, deduplicates exact source records, retains divergent versions and unknown dates, then automatically forms source-backed personal history. The UI offers batch selection and optional message exclusions. It does not authenticate to external accounts, claim access to all of their histories or claim that untouched files have been imported. Import preview/apply protects the selected source bytes; it is not per-entry approval of personal history.


## Feedback and study

`GET /api/feedback` exposes durable feedback status and processing receipts; `GET /api/study` exposes the current reading policy, registered source availability and actual notes. Both are authenticated. A registered video with no transcript is metadata, not a completed reading.

Long-term preferences and existing task requirements are handled through authenticated messages and versioned settings receipts. Topic preferences currently enter generation prompts but do not select a topic-specific retrieval pipeline. Feedback registration does not grant the model code editing, shell, deployment or arbitrary network capabilities. Refer to the current `server.py`, `directives.py`, `feedback.py` and `study.py` for authoritative schemas and enforcement.
