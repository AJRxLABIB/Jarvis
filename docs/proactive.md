# Proactive intelligence (3.20)

`app/core/proactive.py`; runs every 10 minutes while Jarvis is idle (not while it's busy, speaking or waiting for
an approval). It only **tells** you things - a suggestion may carry a command Jarvis wrote ("continue my mission",
"git status", "improve yourself"), which runs only when you pick it, through the normal checks.

## Signals

| Signal | Source |
|---|---|
| goal overdue / due today or tomorrow / untouched for 7 days | your goals |
| mission paused | Mission Control |
| 10+ uncommitted changes in the project you're working on | context engine (git) |
| a tool failed 5+ times in two days | audit log |

## Ranking and delivery

score = 0.45 urgency + 0.35 importance + 0.2 confidence - 0.25 interruption cost (+0.1), clamped to 0..1.

| Level | Tells you when score >= |
|---|---|
| off | never (urgent security/battery still, if "urgent alerts even when off") |
| minimal | 0.75 |
| normal (default) | 0.55 |
| active | 0.40 |
| high | 0.25 |

Then: switched-off categories are dropped; the same thing isn't repeated within 6 hours; during quiet hours
(default 23:00-07:00) and beyond the hourly limit (default 4) a suggestion goes to the notification center only;
modes (gaming, meeting, presentation...) and focus mode keep it silent as before.

## By voice

"be more proactive" / "be less proactive" / "stop suggesting things" / "how proactive are you" /
"why did you suggest that" (urgency, importance, confidence, interruption cost and the rule that let it through) /
"set quiet hours from 10 pm to 7 am". Settings -> Proactive has all options.
