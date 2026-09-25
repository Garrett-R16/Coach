# Coach

A personal endurance coach that lives on your own Linux box. It talks to you over Signal, reads your Apple Health
and Garmin data, keeps its memory as plain markdown and JSON in a private git repo, and runs Claude Code headless
with a deliberately small set of tools. It sends a morning report when your phone syncs, analyses every workout
when it lands, and answers questions in between.

Built for triathlon, written to be sport-agnostic. One athlete per install.

## How it is put together

```
coach-svc (system user, holds the secrets)        your user (holds no secrets)
------------------------------------------        --------------------------------------------------
broker/  deployed copy in /opt/coach              runner/  this repo, plus <COACH_DATA>, your private data repo
  signal-cli  <-> Signal                            watches the queue, runs `claude -p`, writes replies
  health webhook on :8787                           normalises health exports and workouts
  Garmin Connect fetch (ride power)                 morning report, post-workout analysis, chat
  Infisical machine identity
        |  writes   /var/lib/coach/queue/inbox            ^ reads
        +---------- /var/lib/coach/queue/outbox  <--------+ writes
```

- **The broker** is the only process that ever holds a token, the Signal keys, the Garmin session or the vault
  identity. It never runs a model. It runs as a dedicated system user with its files unreadable by anyone else,
  and talks to the agent side only through a directory of JSON files.
- **The runner** runs as you and launches the coach on demand. The coach gets read access to your data
  directory plus seven named functions served over MCP (append to the log, note feedback, rewrite the plan,
  update the profile or schedule, save a note, request a build, send the reply). No shell, no file writing, no
  network. A separate builder agent with code access runs only when you ask for a feature.
- **Your data** is a separate private git repo (`COACH_DATA`): persona and thresholds you write, profile and
  schedule the coach maintains, the plan, a daily log, normalised health data and workouts, and any reference
  material. The coach commits its own changes there, so every decision has a history.

## What you need

- A Linux box that stays on, with systemd, Python 3.12+, and `sudo`. Tested on Ubuntu with Python 3.14.
- [Claude Code](https://claude.com/claude-code) installed and logged in as your user. The coach runs on your
  subscription through `claude -p`; no API key.
- A phone number for the coach's Signal account (a Google Voice number works) and Signal on your phone.
- An iPhone with Apple Health and the [Health Auto Export](https://www.healthyapps.dev/) app.
- Optional: a Garmin Connect account for ride power, an [Infisical](https://infisical.com) project for secrets,
  and [Tailscale](https://tailscale.com) so your phone can reach the webhook without opening a port.

## Setup

Everything below the broker install runs as your user. Commands that need root say so.

### 1. Clone and create your data directory

```
git clone git@github.com:Garrett-R16/Coach.git ~/Projects/coach
cd ~/Projects/coach
scripts/init-data.sh ~/Projects/coach-data
```

`init-data.sh` copies `templates/` into a new private git repo and points the runner at it via
`~/.config/coach/runner.env`. Edit `coach-data/context/profile.md` and `schedule.md` with your goals, races,
history, injuries and weekly availability, or tell the coach later in chat and it will fill them in. Adjust
`persona.md` and `thresholds.md` to taste; the coach reads but never edits those two.

### 2. Register the coach's Signal number

Install [signal-cli](https://github.com/AsamK/signal-cli) (the native build needs no Java) somewhere on your
path, then register the coach's number as yourself. The broker install moves the registration to the service user.

```
# solve the captcha at https://signalcaptchas.org/registration/generate.html, copy the signalcaptcha:// link
signal-cli -a +1XXXXXXXXXX register --captcha 'signalcaptcha://...'
signal-cli -a +1XXXXXXXXXX verify 123456          # code arrives by SMS
signal-cli -a +1XXXXXXXXXX updateProfile --given-name Coach
```

### 3. Secrets

The broker fetches its secrets from Infisical at start with a machine identity, so nothing but that identity sits
on disk. Create a project, add these to its `dev` environment, and create a machine identity with Universal Auth
and the Viewer role on the project:

| name | value |
|---|---|
| `host_number` | the coach's Signal number, E.164 |
| `dest_number` | your number; optional, the first sender is adopted if unset |
| `HEALTH_WEBHOOK_TOKEN` | any long random string, e.g. `openssl rand -hex 24` |

Copy `.infisical.json.example` to `.infisical.json` with your project id (or run `infisical init`).
If you would rather not use Infisical, put the same three variables in `/etc/coach/broker.env` after step 4
and replace `with-secrets.sh` in the service unit with a direct call; the broker only reads environment variables.

### 4. Install the broker (root)

```
sudo scripts/install-broker.sh
```

The first run creates the service user, directories, `/opt/coach/venv` with the one dependency, and an empty
`/etc/coach/infisical.env`, then stops. Put the machine identity's client id and secret in that file and run it
again; it installs and starts `coach-broker.service`. Re-run it after any change under `broker/` or `common/`.

### 5. Install the runner

```
scripts/install-runner.sh
```

Installs `coach-runner.service` and the morning fallback timer as user units and enables linger so they survive
logout. Restart with `systemctl --user restart coach-runner` after pulling code changes.

### 6. Say hello

Message the coach's number on Signal. `/help` lists the commands. The first sender is adopted as the athlete.

### 7. Health data from the phone

In Health Auto Export, create a REST API automation:

- URL: `http://<this-box>:8787/` (its Tailscale name if your phone is on your tailnet)
- Method POST, JSON, header `Coach_Key: <HEALTH_WEBHOOK_TOKEN>`
- Metrics: at least resting heart rate, heart rate variability, sleep analysis, heart rate, steps, active energy.
  Date range "yesterday and today", grouping by hour, hourly schedule.
- A second automation of type Workouts with the same URL and header.

The morning report goes out when the day's first export arrives after 05:00, which is normally minutes after
you unlock the phone. A timer at 08:30 is the fallback; `/morning` forces a resend.

### 8. Ride power from Garmin (optional)

Apple Health carries a Garmin ride's summary but not its power. After the broker is installed:

```
scripts/garmin-login.sh     # asks for email, password and MFA once; keeps only tokens under /var/lib/coach/garmin
scripts/garmin-test.sh      # lists your last three activities to confirm
```

The password is never stored. When a ride arrives from the phone the broker fetches the matching Garmin
activity's power, cadence, heart rate and laps, and the coach waits up to ten minutes for it before analysing.
This uses the community `garminconnect` library, pinned in `broker/requirements.txt`; Garmin changes its login
now and then, so expect an occasional version bump.

## Day to day

- **Morning**: a short recap of yesterday, a recovery check against your thresholds, today's sessions in detail,
  one line to set the tone.
- **After a workout**: summary, the coach's read against the plan, then it asks how it felt. Your answer goes into
  the profile's feedback notes.
- **Chat**: anything. Durable facts go into profile or schedule, plan changes go into the plan.
- `/status`, `/new`, `/workout`, `/build <request>` (the builder agent adds functionality to the harness).

## Where things live

```
context/instructions.md    how the coach works: what to read, the two automatic messages, the functions
context/builder.md         rules for the builder agent
templates/                 starter data directory
runner/                    queue loop, coach invocation, tool server, health normalisation, triggers
broker/                    Signal channel, health webhook, Garmin fetch
common/queue.py            the file queue
systemd/                   one system unit, two user units
scripts/                   install, init, publish, Garmin login and test, secrets wrapper, fake export
```

## Privacy

Your data directory contains health history and everything you tell the coach. Keep it private. GPS is stripped
from workouts before they are stored. The harness repo contains nothing about any athlete; `scripts/publish-template.sh`
exports a history-free copy for sharing. Nothing leaves your box except the messages to Signal, the Claude API
calls the coach makes, and the Garmin and Infisical requests the broker makes.

## Testing

```
journalctl -u coach-broker -f                          # broker
journalctl --user -u coach-runner -f                   # runner
python3 -m runner.triggers chat "hello" --no-send      # run the coach without sending
python3 -m runner.triggers morning --no-send --force
HEALTH_WEBHOOK_TOKEN=... python3 scripts/fake_export.py --workout   # fake phone export
```

## Disclaimer

This is software, not a coach. Nothing it produces is medical, physiotherapy or professional coaching advice.
You use it at your own risk, you are responsible for how hard you train, and you should see a professional
for any injury, illness or health concern. It can be wrong, and it does not know what it cannot see.

## Status and licence

A personal project, in daily use by one athlete. No warranty and no support. The Garmin path is against Garmin's
terms in the strict sense, like every third-party Garmin tool. MIT licensed, see `LICENSE`. Fork freely.
