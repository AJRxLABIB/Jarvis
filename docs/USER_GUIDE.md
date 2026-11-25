# Jarvis user guide

Generated from the real tool list; ask for these in plain words. Each line is a thing Jarvis can do.
Risky actions always ask first. Say "stop" or use the emergency stop at any time.

## Approvals

- **approvals**: Standing approvals: list them, stop asking before a harmless-but-moderate action for a while ('don't ask me again about volume for 30 minutes'), or end them.

## Apps and windows

- **app command**: A command in the app the user is in, sent as its keyboard shortcut: new tab, next tab, full screen, save as pdf, copy, paste, undo, zoom in, go back ...
- **arrange windows**: Arrange windows: put an app (or the current window) on the left/right/top/bottom half, a corner, the middle or full screen; two apps side by side; move a window to the next screen; maximize all; save 
- **close all apps**: Close ALL open apps at once, normally (like clicking X on each; unsaved work still asks).
- **close application**: Close an open application normally (like clicking X; it can still ask to save).
- **open application**: Open (launch/start) an installed application, e.g.
- **restart application**: Restart an application: close it normally and open it again ('restart Chrome').
- **this window**: Close, minimize, maximize or name the window the user is working in ('close this').
- **window control**: Switch to, minimize, maximize or restore an application's window; minimize_all shows the desktop.

## Browser pages

- **browser back**: Go back one page in the assistant's browser.
- **browser click**: Click a link or button (by its number from the last read, or its text).
- **browser close**: Close the assistant's browser.
- **browser open**: Open a web page in the assistant's own browser and read it: use this whenever you need to see what is on a page, summarise it, or click/type on it.
- **browser read**: Read the current page in the assistant's browser: text and numbered links/buttons/fields.
- **browser tabs**: List, switch, open or close tabs in the assistant's browser.
- **browser type**: Type text into a field on the page; submit=true presses Enter.

## Clicking and typing

- **click text**: Click (or double/right-click) visible text on the screen, e.g.
- **click thing**: Click something on the screen described in words - an icon, picture or button without readable text, e.g.
- **mouse control**: Click, double-click or right-click where the pointer is, move it, or center it.
- **press control**: Press a button, link, tab, menu item or check box BY ITS NAME in the window the user is using (Windows accessibility, no screen reading): 'press Save', 'click OK', 'tick Remember me'.
- **press keys**: Press a keyboard shortcut in the active window, e.g.
- **scroll screen**: Scroll the active window up or down.
- **type text**: Type text into the field that has focus (never passwords).

## Clipboard

- **clipboard ai**: Work on the text the user copied with the local AI: summarize, translate, fix grammar, explain, write a reply, shorten - and put the result on the clipboard ('translate what I copied').
- **clipboard history**: The user's opt-in clipboard memory: search or list what they copied earlier, put an item back on the clipboard, pin it, or erase it.
- **copy to clipboard**: Put text on the clipboard (e.g.
- **read clipboard**: The text the user copied (for 'translate / summarise / explain what I copied').

## Development

- **add project note**: Remember a known problem or note about a project.
- **check project**: Run the project's tests (or build) with its own command and summarise what failed.
- **code impact**: Before changing a function, class or file: where it is defined, what refers to it, which project files import it and which tests are related.
- **dev environment**: Check which developer tools are installed (git, node, npm, python, pip, VS Code).
- **dev session**: Start the user's project the usual way AND check that it really came up: whether it is still running, whether its port answers, what errors its log shows (missing package, port in use, syntax error, .
- **edit code**: Change a file in a project by exact replacements (each 'find' must appear once), or create a new file.
- **exec lab**: The execution laboratory: see which isolation this PC really offers, plan how an operation would run (or why it cannot), copy a file or project into an experiment workspace (secrets left out), statica
- **experiment**: Try risky or large changes safely in a separate copy of a git project (its own folder and branch): action start / list / status / merge / discard.
- **git commit**: Commit all changes in a project with a message (stages everything first); push only if the user asks.
- **git log**: Show the most recent git commits of a project.
- **git status**: Show git status (branch and changed files) of a project.
- **github**: Read-only GitHub: your unread notifications, issues and pull requests assigned to you, or a repository's open pull requests.
- **open in editor**: Open a project folder in Visual Studio Code.
- **project move check**: What would break if a project folder moved (e.g.
- **project profile**: What a project is: stack, dependencies, entry points, how to run / test / build it, database, git state and known problems - read from its own files.
- **read code**: Read lines of a file in a project, with line numbers (before editing it).
- **restore code**: Restore a file from the backup Jarvis made before changing it.
- **review changes**: Review the uncommitted changes of a project before committing: which files changed, and anything risky (removed or skipped tests, secrets, huge deletions).
- **run command**: Run ONE development command in a project folder, e.g.
- **run project**: Start / run the user's project the usual way ('run my project'): npm run dev, manage.py runserver, php artisan serve, python main.py...
- **safe edit**: Change code with a safety net: run the project's tests for a baseline, apply the edits, run the tests again, and keep the change only if they still pass and none were lost - otherwise roll it all back
- **search code**: Search a project's code for text (a function, a variable, an error message); gives file:line matches.
- **security scan**: Check a project for common security mistakes: secrets in code, eval/exec, shell=True, unsafe loading, TLS checks off, secret files git would commit, unpinned dependencies.

## Everyday basics

- **calculate**: Calculate a math expression with numbers, e.g.
- **diagnose jarvis**: Check Jarvis's own health: database, disk space, stuck requests/missions, recent errors, crashes, emergency controls, code caches, local AI.
- **emergency control**: Emergency: stop everything now, or switch off automations / the phone remote / network actions, or lock Jarvis to read-only.
- **explain action**: Explain why Jarvis did (or didn't do, or asked about) its last action, from the recorded audit trail: which actions ran, their risk, why it asked, what you answered, whether it was verified.
- **get datetime**: Get the current local time, date and weekday.
- **list undoable**: What Jarvis can undo right now.
- **model status**: Which AI models Jarvis has and uses: main, fast, smart, coding, vision, installed ones, and which model answered the last request (and any fallback).
- **notifications**: The notification center: what's new (reminders, alerts, missions, security), how many, mark all read, or clear.
- **repair jarvis**: Fix Jarvis's own bookkeeping after a crash: close requests left running, pause stuck missions, end idle phone sessions, clear out-of-date code caches.
- **undo last action**: Undo Jarvis's last reversible action (move/rename/create a file, volume, brightness).

## Files and folders

- **batch rename**: Rename many files at once by a rule (replace text, prefix, suffix, numbering).
- **convert image**: Convert an image to PNG, JPG or WebP (a new file next to it).
- **create item**: Create a new folder, or a new text file, e.g.
- **data refinery**: Scan a folder for deadlines, tasks, decisions and labelled values (rules, local), keep each with its source file and line, flag stale facts and conflicting values, and review before anything is truste
- **delete item**: Delete a file or folder by moving it to the Recycle Bin.
- **empty recycle bin**: Empty the Windows Recycle Bin (permanently deletes what is in it).
- **extract zip**: Unpack a .zip into a new folder next to it (unsafe archives are refused).
- **file info**: Get size, type and dates of a file, or the total size of a folder.
- **find duplicates**: Find identical (duplicate) files in a folder and how much space the extra copies use.
- **find files**: Search for files by type (pdf, docx, jpg...), part of the name, what they are about (words inside the file) and how recent, in Desktop, Downloads, Documents or a given folder.
- **find my files**: Find the user's files in Desktop, Documents and Downloads by what they are about, type and date ('the PDF about machine learning I downloaded last week'), optionally open the best one.
- **list folder**: List what is inside a folder.
- **merge pdfs**: Merge several PDFs (in the given order) into a new PDF.
- **move item**: Move a file or folder to another folder, or copy it (mode='copy').
- **open path**: Open a file (in its default app) or a folder (in File Explorer), e.g.
- **organize desktop**: Tidy the Desktop: move loose files into 'Organized Desktop' folders by type (images, documents, videos...).
- **organize downloads**: Sort the Downloads folder: loose files go into folders by type (Documents, Images, Installers ...).
- **rename item**: Rename a file or folder (give only the new name).
- **shrink images**: Make smaller copies of photos (for sending): '...-small.jpg' next to each original.
- **vault scan**: Read a folder of Markdown notes (Obsidian vault): note count, tags, links, backlinks and links to notes that do not exist.
- **verify claim**: Independently check a claim that something was done by looking at the real result: a file that was saved (exists, size, recent, expected text, can be reopened) or a Git operation (branch, latest commi
- **word to pdf**: Convert a Word document to PDF (needs Microsoft Word installed).
- **write document**: Save a document (Word .docx by default) with the results of a task: headings, bullet points and tables written in simple markdown.
- **zip items**: Put files or folders into a new .zip next to them.

## Memory, knowledge and learning

- **answer sources**: Where the last answer's facts came from: which tools ran (and whether their result was checked), which saved memories matched, and what was only the AI's wording ('where did you get that?', 'is that v
- **ask documents**: Answer a question from the user's own indexed documents (PDF, Word, notes): returns the most relevant passages with file names.
- **ask history**: Answer 'when did I last ...' and 'how many times did I ...' from Jarvis's own record of what it did and the to-dos you finished (with the records it is based on).
- **ask knowledge**: Answer from the user's own knowledge graph (projects, files, decisions, memories, goals, missions, requests), with sources: 'which files belong to TMS', 'what decision did I make about the database', 
- **cognitive plan**: Assess how much thinking a request needs (level 1-5 with a budget), split it into ordered objectives, name the specialist roles to use and say whether one short question must be asked first.
- **current context**: What the user is doing right now: the app and window they're in, the project, file, git branch and uncommitted changes, goal, mission, approval waiting, PC state - and how sure Jarvis is.
- **deadline risk**: List what is overdue, due soon, behind pace, blocked or overloaded across the user's todos and goals, with the rule behind each ('what am I late on', 'deadline risks').
- **doc to plan**: Read a document's recommendations, requirements and claims and check each against the real Jarvis source, tests and docs; returns gap-first proposals with files, tests and acceptance criteria.
- **experiment log**: Keep a scientific record when diagnosing a cause: hypothesis, the single change, baseline, observations, and a conclusion graded 'correlation only' / 'supported' / 'contradicted' - never more certain 
- **explain memory**: Why Jarvis remembers something and where it came from ('why do you remember that?', 'where did you learn my project folder?').
- **forget**: Delete one saved fact when the user asks you to forget it ('forget that', 'forget my project folder').
- **forget everything**: Delete ALL saved facts.
- **forget history**: Delete all saved conversation history (long-term memory of our chats).
- **index documents**: Index (or refresh) the user's document folders so ask_documents can search them.
- **inference queue**: Show the AI request queue: what is running or waiting, the device each used, recent latencies and scheduler holds ('what is the AI busy with').
- **learn**: Learning with spaced repetition: add questions (yours, from a document, or AI-written and marked so), quiz what is due, record answers (graded by word overlap unless the user says how well they knew i
- **learning**: What Jarvis has learned from using it (skills, lessons from corrections), whether it is improving week by week, or forget all of it ('what have you learned', 'are you getting better').
- **learning audit**: Check what Jarvis learned from use for items that made things worse, contradict a better one or faded away; clean them up (reversible) ('audit your learning', 'is anything you learned wrong', 'clean u
- **location status**: Show the saved places (names and radii only, never coordinates), the location rules and the last place event ('where are my saved places', 'location rules').
- **manage memory**: Control one memory: stop using it, use it again, make it private, let it expire, or show its earlier versions.
- **manage places**: Add or remove a saved place, add / remove a rule that says something when you enter, leave or stay at it, or send the saved places to the phone so it watches them ('save home as a place', 'when I arri
- **media manage**: Register recordings in a folder, transcribe and index them in the background (needs FFmpeg), cancel the job, or remove a recording from the index.
- **media search**: Search what was said in the user's indexed recordings (lectures, meetings, videos) with file names and timestamps, list a recording's chapters, make study notes from it, or show indexing status ('find
- **memory admin**: Memory housekeeping: list categories, export all memories to a file, stop or resume using a whole category, or delete a category.
- **memory conflicts**: Memories that may contradict each other (e.g.
- **memory merge**: Review-first memory consolidation: list proposals to merge two memories that say nearly the same thing, apply one (the second memory is switched off, not deleted), reject it, or undo an applied merge 
- **memory suggestion**: Accept or reject the waiting memory suggestion, or list them.
- **memory used**: Which saved memories Jarvis gave the AI for its last answer, and why each one ('why did you mention that?', 'what do you remember that you used just now?').
- **opportunities**: Evidence-backed improvement opportunities from Jarvis's own records: repeated manual work, tools that keep failing, paused missions, goals without a plan, overdue promises ('what could be improved', '
- **predictions**: Jarvis's own prediction record: write down what is expected before an action, record what really happened, and report how accurate past predictions were ('how good are your predictions', 'were you rig
- **recall**: Look up what the user asked you to remember ('what do you remember about me?').
- **recall activity**: What Jarvis did for the user in a time period (apps and sites opened, music, searches), from its own log.
- **remember**: Save a fact ONLY when the user explicitly asks you to remember something.
- **request context**: Show the evidence behind a request: everything Jarvis merged as relevant to it - what you are doing, projects, memories, missions, devices, resources, permissions - each labelled verified / retrieved 
- **resolve memory conflict**: Settle a memory contradiction: keep the new one, the old one, or both.
- **retrieval sources**: Control what Jarvis may index and search: exclude folders or patterns, leave memories / documents / conversations out of the combined search, list near-duplicate documents, purge excluded material fro
- **root cause**: Diagnose why a run failed: the earliest failing step, the possible causes ranked by evidence (never certain), later failures marked as possible consequences, and recovery options with a level ('why di
- **schedule whatif**: Simulate the user's to-do schedule without changing it: 'can I finish everything this week', 'what if I postpone task 4 by 3 days', 'what if I study 2 hours a day'.
- **search everything**: Search what the user told you to remember, their indexed documents and past conversations together; one ranked list that says where each result came from.
- **search history**: Search our past conversations by meaning ('what did I say about my thesis last week?').
- **second opinion**: Double-check an answer by asking the local model the same question several times and keeping what most runs agree on; says UNCERTAIN when they disagree.
- **tech decision**: Remember why a technology or library was accepted, rejected or deferred, and when to look again; lists decisions that are due for a re-check so an old rejection is revisited instead of repeated ('reme
- **tech experience**: Technical problem memory: find a similar past problem with what failed, what worked and whether it applies to the current environment; record a solved problem ('have we seen this error before', 'remem
- **tune ai**: Test better set-ups for Jarvis's own AI (another installed model, a more exact setting) on the user's real requests and keep one only if it scores better ('tune your AI', 'make yourself smarter', 'upg
- **verification coverage**: Show which tools verify their own results and which only report success ('which actions are verified', 'what can't you double-check').
- **voice data**: The user's own voice recordings kept to train speech recognition (SPEECH_DATASET): report how much is kept, export the corrected ones for training, or erase them ('forget my voice recordings').
- **what can you do**: List Jarvis's real capabilities (the registered tools, by area).
- **what do you know**: What Jarvis knows about the user's environment - projects (and their stack), goals, the unfinished mission, most-used apps, contacts' names, layouts, nicknames - and where each fact comes from.

## Money

- **expenses**: Personal expense records and monthly budgets, kept on this PC: add what was spent, list, this month's report by category, 6-month trend, set or show budgets, delete a record, export to CSV.
- **finance plan**: Money planning, all local: preview/import a spending CSV, recurring payments with reminders, savings targets, a month-end forecast with its assumptions, and what-if scenarios.
- **import bank sms**: Expenses from the bank / bKash / Nagad SMS on the user's phone: preview the payments found in the latest messages, or import them (never the same message twice).

## Notes

- **meeting notes**: Start or stop taking notes of a meeting, class or lecture: everything said is written down and summarised at the end.

## Office, mail and calendar

- **calendar conflicts**: Find overlapping appointments in your Outlook calendar today, tomorrow or this week.
- **calendar events**: Events on the user's Outlook calendar today, tomorrow or this week.
- **commitments**: Promises the user made and things they are waiting for from others, found in mail and kept until settled: list them, scan recent mail, add one, or mark one done / dismissed ('what did I promise', 'who
- **email draft**: Write, list, show, edit, approve or discard email drafts (stored locally, never sent from here).
- **excel control**: Microsoft Excel on this PC: list sheets, read cells, write values, add a row, create a workbook ('add 250 lunch to my expenses sheet').
- **google**: Google Calendar and Drive (your own Google sign-in): list upcoming events, add an event (asks first), search Drive file NAMES (never contents), connect or disconnect Google.
- **needs me**: One ranked list of what needs the user now across unread mail, WhatsApp, phone notifications, promises and to-dos, with the reason for each and which sources could not be checked ('what needs my atten
- **read emails**: The latest (unread) emails: sender, subject, preview.
- **send email**: Send an email (always asks first, showing the full message): from the mailbox set up in .env over SMTP when IMAP_USER / IMAP_PASSWORD are set, otherwise from Outlook.
- **triage emails**: Sort recent email into urgent / routine / promotional / possibly suspicious with reasons, deadlines and requested actions.

## PC health and security

- **action evidence**: What Jarvis has independently VERIFIED versus only reported: verdict counts, recent evidence records, the verdict for one action, and which independent check fits a kind of action ('what did you actua
- **activity timeline**: What Jarvis did and who started it (you by voice/typing/phone, a scheduled automation, or Jarvis), with whether each result was verified; search, list unverified changes, export, or permanently delete
- **anomalies**: What looks statistically unusual for you right now: tools that got slower or started failing, PC load, and spending far above your usual - measured against your own history (median / MAD), with too li
- **capability report**: What Jarvis is really missing: recurring failures classified by cause (missing tool, permission, information, model limit, platform...) with improvement proposals, or an inventory of tools with risk, 
- **clean temp files**: Delete temporary files older than 2 days to free disk space.
- **device mesh**: List every device Jarvis knows (this PC, other computers, paired phones) with connection, trust, battery, what each can do and what is waiting on it; 'discover' also looks for JARVIS devices on the lo
- **digital twin**: Show what Jarvis last observed about your environment (devices, missions, disk, memory) with source and age - stale readings are marked - or preview a plan/mission BEFORE it runs: what it would touch,
- **end process**: Force-end (kill) a running process.
- **file attribution**: Which PROGRAM changed, created or deleted files, from Windows' own audit / Sysmon records when the user has switched them on, or how to switch them on ('which program changed files in my Documents', '
- **gesture control**: Control the mouse with hand gestures from the webcam: enable/disable/pause/resume, status, calibrate, settings, list gestures, map a gesture to a tool, diagnostic.
- **guardian**: The cyber guardian: is anything unusual happening on this PC (a burst of file changes, a new startup program, a new network listener, a stale or failed backup)?
- **health history**: What is normal for this PC (typical CPU and memory), or forget the recorded health readings (all, or older than N days).
- **idle work**: What Jarvis's idle-time housekeeping actually did while the user was away (with real results), or run the due jobs now ('what did you do while I was away', 'run idle work').
- **integrations**: Which outside services are connected (Home Assistant, the local AI, MCP servers, the phone remote, Outlook, GitHub, the browser), what each one's last smoke test found, and how to connect the missing 
- **investigate**: Find out why the local AI model isn't answering by checking the cheapest evidence first (is Ollama up, is the model installed, is there enough free memory, recent errors) and stopping when a cause is 
- **lease**: Mission-scoped permission leases: limit a mission to listed tools, a folder, a time and a number of calls; show or revoke a lease ('limit this mission to read-only tools', 'revoke the mission's permis
- **list processes**: List running programs sorted by CPU or memory use.
- **pc checkup**: A read-only check-up of the PC's protection and parts: antivirus, firewall, Windows updates, pending restart, disk health and wear, battery wear, start-up programs, free space - each ok / attention / 
- **pc health**: How the PC has been doing over the last day (CPU, memory, disk, battery) and anything that looks wrong: sustained high CPU, memory growth, a nearly full disk, failed automations, stuck missions, faili
- **performance mode**: Performance mode: Fast, Balanced, High quality, Battery saver or Auto.
- **program history**: Which programs use more than their own usual, and which program was heaviest just before failed requests (a correlation, labelled inferred).
- **radar feedback**: Tell the proactive radar about an alert: snooze, mute, unmute, useful / not useful (three 'not useful' mute it for a week), or scan to see what it would raise now.
- **screen watch**: Watch a window or the screen and tell me when it changes or a phrase appears.
- **service breakers**: Which online services Jarvis has paused after repeated time-outs or connection failures, and reset them ('why is web search paused', 'try the services again').
- **settings profile**: Switch between named sets of everyday settings (meeting, night, performance, or your own): list, show, save the current ones, apply, undo, delete.
- **system status**: Measure CPU usage, RAM/memory usage, disk space, battery level, network speed, uptime or GPU.
- **task replay**: Debug a task that went wrong: rebuild what happened from Jarvis's own record (facts marked as recorded vs reconstructed, missing data named), find the earliest divergence, suggest a minimal recovery, 
- **trace export**: Export one request's recorded trace as an OpenTelemetry (OTLP/JSON) file in Jarvis's exports folder, for an external trace viewer.
- **trace request**: Show everything recorded for one request (by its ID, or the last one): actions with risk and decisions, mission/task state changes and events.
- **troubleshoot**: Find setup problems (Python version, missing packages, missing models, microphone, missing .env, database version, services not answering) with evidence, cause and the fix.
- **voice stats**: How voice interaction has been going (recognition time, time to first reply, low-confidence and no-command wakes, per language).
- **work snapshot**: Remember a working session, compare it with the real PC later (what changed or is gone), resume it safely, or show unfinished missions after a restart with what is uncertain.
- **workspace**: Named workspaces that join a project folder, files, a saved window layout, a goal and notes.

## Routines, missions and automation

- **background mode**: See or control the optional windowless background host that runs scheduled automations: status, pause, resume or stop.
- **build tool**: Create a NEW reusable tool from a request: a workflow of existing tools with named inputs.
- **cancel mission**: Cancel the unfinished mission.
- **delegate**: Give one specialist agent a short list of tool calls to do now, scoped to what that agent is allowed to do.
- **demo record**: Teach a routine by clicking: record which NAMED controls the user clicks during a session they start (never keys, never pictures), show the draft, and save it as a workflow only when asked ('record my
- **experience**: Find routines you repeat (the same successful tool steps in the same order on different days) from Jarvis's own action record and offer them as workflows; saving asks you, dismissed ones aren't offere
- **export workflow**: Export a saved workflow to a portable JSON file (data only) that declares the tools and risk it needs.
- **goal check**: Read-only: what a request means, what would count as done, what is being assumed and what really needs asking first; with no request, whether the latest mission actually met its goal, judged from its 
- **goal review**: Long-horizon goals: progress from finished tasks, pace from your own history, a rough forecast against the deadline, proposed replans (nothing is changed), and a weekly review.
- **handoff**: Move a mission between this PC and a paired phone: create a signed, expiring package (step titles and states only, no secrets), inspect or accept one a device sends back (it is checked against the mis
- **import workflow**: Import a workflow file (.jarvis-workflow.json): checked strictly, saved as a manual workflow, never run by importing, never more powerful than it declares.
- **list agents**: The specialist agents you can delegate to (researcher, planner, organizer, coder, operator, secretary) and what each may do.
- **list automations**: List reminders, scheduled automations and workflows.
- **list goals**: The user's active goals with progress, deadlines and next tasks.
- **manage automation**: Pause, resume, delete or run now an automation or reminder.
- **mission history**: The state changes of the most recent mission (started, paused and why, verified, completed), so you can see what happened.
- **mission report**: After a mission stops: what failed and the probable cause, which steps succeeded, what remains, whether a retry is safe or needs a check first, and another way forward; or statistics across missions (
- **mission status**: The recent missions, how far they got and their milestones.
- **restore workflow**: Bring back an earlier version of a workflow or automation (the current one is kept as a version too).
- **resume mission**: Continue the unfinished mission (after a pause, failure or restart); skip_failed=true skips the failed step.
- **rollback mission**: Undo the finished steps of the most recent mission, newest first, where each can be undone; steps that can't be undone are reported, not faked.
- **routine suggestions**: Routines the user repeats at the same time on several days (from Jarvis's own log), with an offer to make one a saved automation.
- **run team**: Split a job across several agents: each step names an agent and a tool call.
- **run workflow**: Run a saved workflow, e.g.
- **save workflow**: Save a named multi-step workflow the user can run later.
- **schedule task**: Create an automation that runs tools on a schedule (e.g.
- **set goal**: Save a goal the user is working towards (with deadline, priority and optional tasks), e.g.
- **set reminder**: Remind the user of something at a time, in N minutes, or repeatedly (daily/weekdays).
- **simulate workflow**: Play a saved workflow through a what-if (a step fails, times out, is denied, loses its answer, the program crashes and restarts, it is triggered twice, or the emergency stop is pressed) using pretend 
- **skill card**: Show one saved routine / workflow as a card: its inputs, the tools, apps and abilities it needs, the risk of each step, what happens if a step fails, which steps can be undone, how often it worked, an
- **snooze reminder**: Snooze the reminder that just went off (or a named one): remind again in N minutes.
- **start mission**: Plan and run a multi-step task as a mission that survives restarts: give the steps as tool calls in order.
- **task center**: Background task center: missions, scheduled automations, self-improvement tests and requests still running, each with a status (queued, running, waiting for approval, paused, completed, failed, cancel
- **teach workflow**: Teach Jarvis a routine by showing: start a session, do the routine by telling Jarvis what to do, then stop to save what it actually did as a workflow (asks first).
- **update goal**: Change a goal: add a task, tick a task off, set progress, mark it done or drop it.
- **workflow versions**: Earlier versions of a workflow or automation (kept when it was changed or deleted).

## Screen and photos

- **analyze photo**: Analyse a photo of the real world (an object, a device, a document, a screen of lights): the text read from it plus the vision model's description, clearly labelled as a guess; or compare two photos (
- **camera session**: Open, close or check the explicit photo session used to analyse photos from the phone ('start camera', 'stop camera').
- **describe screen**: Look at the active window, a named app window, or the whole screen and describe it or answer a question about it (uses a local vision model; falls back to reading the text).
- **read screen**: Read the text shown in the active window, a named app window, or the whole screen (OCR).
- **screen history**: Opt-in screen history: keeps the TEXT of the window in front (never pictures) so the user can ask what was on screen earlier; start, stop, search, status, or delete everything ('what was that error I 
- **sound monitor**: Start, stop or check listening for configured sounds ('tell me when someone knocks', 'stop listening'); local, nothing recorded, not an emergency system.
- **take screenshot**: Save a screenshot (PNG) of the active window, a named app window, or the whole screen.

## Self-improvement

- **apply proposal**: Apply a TESTED proposal to Jarvis's real source (backup first, verified).
- **improve yourself**: 'Improve yourself': look at what keeps failing (real evidence from the last 14 days) and list improvement proposals.
- **list proposals**: List Jarvis's improvement proposals and their state.
- **propose code change**: Save an exact code change to Jarvis's own source as a proposal (find/replace edits).
- **reject proposal**: Reject an improvement proposal.
- **rollback proposal**: Undo an applied proposal from its backup (the last one if no number is given).
- **show proposal**: Show one improvement proposal: why, evidence, the diff, the test report.
- **test proposal**: Test a proposal on a copy of Jarvis's source (full test suite + linter).

## Smart home

- **home control**: Turn a smart-home device on/off (lights, fan, switch, TV, curtains, lock) via Home Assistant.
- **home status**: Which smart-home devices exist and what is on.
- **local device**: Switch smart plugs / relays (Shelly, Tasmota) and WLED lights on this network directly (no cloud): on, off, toggle, brightness, status, list.

## To-do and focus

- **add todo**: Add something to the user's to-do list, optionally with a due day.
- **command center**: Your day at a glance: overdue and due to-dos, blocked items, active missions and automations, what is waiting for you, and what you finished today ('how is my day', 'summarize what I completed today' 
- **complete todo**: Mark a to-do item done (by number or words).
- **delete todo**: Remove an item from the to-do list without doing it.
- **focus mode**: Focus mode: quiet time for N minutes (no suggestions or non-urgent alerts), optionally closing distracting apps; stop it or say how long is left.
- **list todos**: What is on the user's to-do list.
- **plan my day**: Plan today from the to-do list, time estimates and calendar: ordered items with reasons, the next action, what doesn't fit, blocked items and conflicts.
- **update todo**: Change a to-do item: postpone it (a day or to a date), set its due day, or set its priority (high / normal / low).

## Voice

- **accept fix**: Accept a fix from the mistakes review (by number, or 'all').
- **change setting**: Change one of Jarvis's settings by voice (screen capture, voice speed, language, hands-free follow-up time, speak replies, only my voice ...).
- **dictation**: Start or stop dictation: while on, everything the user says is typed where the cursor is.
- **interpreter mode**: Interpreter: translate everything the user says (Bangla <-> English, or into a named language) until they say stop.
- **jarvis control**: Jarvis's own screens and features: settings, memory, privacy, logs, automations, the system page, voice enrollment, wake word, voice training, the Jarvis model, self-test, speed report, updates, phone
- **list nicknames**: The user's nicknames for apps, sites and pages.
- **proactive control**: How proactive Jarvis is: status, why it suggested something, set the level (off / minimal / normal / active / high), one step more or less, quiet hours.
- **remove nickname**: Remove one of the user's nicknames.
- **review mistakes**: Review this week's misunderstood, cancelled and corrected requests and suggest fixes.
- **set mode**: Switch Jarvis's mode (normal, work, coding, research, gaming, presentation, meeting, private, maintenance) or tell the current one.
- **set nickname**: Give something a nickname the user can say: an app, website, Settings page or workflow ("call Visual Studio Code 'code'").
- **study mode**: Study / quiz mode: ask the user questions on a topic, check the answers, keep score.

## Weather, news and media

- **daily briefing**: A short briefing: greeting, time and date, weather, battery, today's reminders, to-dos, meetings and unread email (Outlook), and what you worked on last time ('good morning', 'brief me', 'what's my da
- **environment report**: The surroundings today for a city: air quality (AQI, PM2.5), UV, feels-like heat, rain in the next hours, daylight - with what to do about each ('how is the air today', 'do I need a mask', 'is it safe
- **get news**: Today's top news headlines (optionally about a topic), in English or Bangla.
- **get weather**: Current weather or today's/tomorrow's forecast for a city (default: the user's home city).
- **inference providers**: Show which local AI engines are set up (Ollama, Foundry Local), whether each is answering and which models they offer ('which AI engines do I have').
- **media control**: Control music/video that is ALREADY playing in any app: pause, resume, next or previous track, stop.
- **play music**: Start playing music now: a song, artist or kind of music (or anything, if none is named).
- **youtube control**: Control the YouTube video/music playing in the browser: skip ad, like, seek forward/back, restart, full screen, captions, mute the video, what song is playing, queue a song to play after this one, sho

## Web

- **a2a manage**: Register an outside A2A agent, approve or block it, remove it, or send it one task.
- **a2a status**: List the outside AI agents registered with Jarvis, their skills and whether each is approved ('which agents do I have', 'agent status').
- **find app api**: Search the web for a program's official API or SDK documentation and return the links as unverified leads ('does Spotify have an API', 'find the Notion SDK').
- **open website**: Open a website or web app for the user: YouTube, Facebook, Instagram, Gmail, a domain or URL.
- **research**: Research a question properly: search the web, READ the top pages and answer from them with numbered sources.
- **research project**: Keep a named research project: question, sources (url, date, text), claims marked sourced or inference, conflicts, a conclusion; show the cited report; check_updates re-reads the sources and says what
- **search in browser**: Open a Google or YouTube search page in the user's own browser for them to look at.
- **summarize video**: Summarise a YouTube video from its captions (a link, or the video Jarvis is playing).
- **web search**: Search the internet and get the results (titles, links, snippets) to answer a question, e.g.

## Wellness and journal

- **decision**: Decision records and option comparison: record what was chosen over what and why, correct or delete it, note how it turned out, find earlier decisions about a topic (context, not rules), or compare op
- **habits**: Habits you tick off daily (drink water, exercise...): add/remove a habit, mark done or undo, what's left today, streaks, last-7-days report.
- **journal**: Your private journal / voice notes on this PC: add an entry (with an optional mood), list recent ones, search them, show today's, delete one.
- **preferences**: Your preferences for how Jarvis works: save one you state ('keep responses shorter'), list / confirm / reject / remove them, see what applies in a profile, or switch learning off.
- **research brief**: A sourced research brief from your own memories, decisions, indexed documents and knowledge graph (offline), marking outdated and conflicting information; also documentation gaps and proposed (unconfi
- **wellness**: Optional wellness log (off until you enable it): record sleep, exercise, breaks, energy, mood; see averages and patterns with their working.

## Windows settings and devices

- **app adapter**: How Jarvis can drive a desktop program: how well it exposes controls, which structured tool to prefer, known actions, and the safest route for one action ('how can you control Excel', 'how would you s
- **attention**: Would Jarvis interrupt you right now, and which background work does the current mode allow?
- **audio output**: Change where sound plays: put it on the PC speakers, headphones, a Bluetooth / connected speaker, or a named device.
- **brightness control**: Get or change the screen brightness: set a level, turn it up (brighter) or down (dimmer).
- **device toggle**: Really switch Wi-Fi, Bluetooth or dark mode on or off (or tell whether it's on).
- **discover integration**: Find how a program on this PC could be driven: an existing Jarvis tool, a command-line program, COM automation, a URL scheme, or UI Automation - ranked, with what each can and cannot do ('how could yo
- **interaction advice**: Which way fits right now - voice, keyboard, gestures or your phone - and whether replies should be spoken (meeting, full-screen app, quiet / sleep mode).
- **investigate problem**: Investigate a problem with Jarvis or the PC from the evidence (logs, resources, checks, events): ranked causes marked CONFIRMED / STRONGLY_SUPPORTED / POSSIBLE / UNKNOWN, safe next steps, and a rechec
- **office info**: Describe a Word, Excel or PowerPoint file without opening Office: word count and headings, sheets and formulas, slide titles.
- **open settings page**: Open a Windows Settings page or a system tool by name: bluetooth, wifi, display, sound, notifications, power, storage, apps, default apps, startup apps, background, dark mode, taskbar, date and time, 
- **phone context**: What the paired phone says about its surroundings (Wi-Fi or mobile data, headphones, orientation, still or moving, light) and how old that is ('is my phone on Wi-Fi', 'does my phone have headphones co
- **place task**: Recommend which of the user's trusted devices should do a task (PC, phone, other computers) from abilities, memory, GPU, battery, privacy and load, and say why the others were ruled out.
- **plugin check**: Scan the plugin files for network, process, native-code and dynamic-execution use before they are trusted ('check my plugins').
- **power control**: Lock the PC (shows the Windows lock screen: 'lock my PC/laptop/screen'), sleep, sign out, restart or shut down the computer (save_work=true for 'save and restart'), or cancel a restart/shutdown.
- **privacy zones**: Show or change privacy zones: password managers, banking, private browsing and meetings (and your own) make Jarvis stop capturing the screen, remembering, saving the clipboard and letting a phone cont
- **resources**: Show GPU memory, CPU, RAM, the AI models currently loaded and whether a model would fit ('how much GPU memory is free', 'what models are loaded', 'will this model fit').
- **retention**: Show what Jarvis keeps (notifications, event log, clipboard memory, health readings, recorded activity, finished tasks, closed incidents), how long, and clear one of them on request.
- **screen recording**: Start or stop recording the app in front (Xbox Game Bar, saved in Videos\Captures; the result is checked).
- **sensors**: Read the room / door / motion / temperature sensors on the local MQTT broker (ESP32, ESPHome, Zigbee2MQTT).
- **service impact**: When something has failed (or you ask 'what if the AI engine stops'): which parts of Jarvis still work, which are limited and which are unavailable, with the recovery level that applies.
- **situations**: Group recent related events (a failed build, its changes, the errors) into one explained situation with next steps.
- **transaction**: Coordinate one task across the PC and a paired phone: steps with an owner device, dependencies, checkpoints, honest completion (a phone step counts only with a result the phone returned) and recovery 
- **ui drift**: Check whether a desktop program's interface changed since Jarvis last learned it, and find the likely replacement for a control that moved or was renamed ('did Excel change', 'where did the Save butto
- **volume control**: Get or change the system volume: set a level, turn it up or down, mute or unmute.
- **vscode**: Control VS Code: open a file at a line, open a folder, compare two files, list installed extensions.
- **what changed**: Say what changed while you were away: events, notifications, missions, files in watched folders, devices and security incidents since a time you give (or since you were last active).
- **windows update**: Windows Update: install (check, download and install updates now), check (only look), or list the updates that are waiting.

## Your phone

- **add device**: Add a device (name, MAC address, network address) so Jarvis can check it and wake it.
- **call contact**: Call a saved contact through the user's phone (Phone Link).
- **check device**: Is a device on right now (does it answer on the network)?
- **device info**: What a device can really do (detected, not assumed): lock / sleep / restart / shutdown / hibernate, remote wake from sleep or shutdown, and for this PC exactly what remote power-on needs.
- **list contacts**: The names of the saved contacts.
- **list devices**: The user's devices (this PC and ones they added) with their state.
- **mobile sync**: Choose which data a paired Android phone may sync with this PC (to-dos, generic notifications) or turn it off or delete the phone's sync data.
- **phone control**: Your paired phone: battery status, flashlight on/off, ring it (find my phone), open an app, the gallery, camera, messages, dialer or a link on it, show a message on it, start sharing its screen, call 
- **read messages**: Read the phone's messages or notifications (not possible: explains why).
- **read whatsapp**: Unread WhatsApp chats (who wrote, how many, the last message) from the WhatsApp desktop app.
- **remove device**: Remove a device from the list.
- **save contact**: Save a contact's phone number (on this PC).
- **text contact**: Start a text message to a saved contact (the user presses Send).
- **wake device**: Wake another device with Wake-on-LAN and CHECK that it came online (never claims it's on without seeing it).
- **whatsapp message**: Send a WhatsApp message to a saved contact through WhatsApp Desktop.

## What is not verified yet

These are built and tested with fakes, but have not been checked on real devices:

- Reading and sending mail on a real mailbox (set IMAP_USER / IMAP_PASSWORD or the SMTP settings first).
- Interface-change detection (`ui_drift`) on real programs, and the Windows file-audit events (need admin and recording on).
- Screen history and click teaching on Windows; the object detector on real photos.
- Phone features on a real phone, including the Android build.
- The 3D core on your graphics driver (if it crashes, update the driver or set VISUAL_QUALITY=balanced).
- How well the small local models handle your own questions: run `python run.py --longmem-eval --full` and `--ground-trial <vision model>`.
