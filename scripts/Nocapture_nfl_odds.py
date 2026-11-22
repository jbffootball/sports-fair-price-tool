
#!/usr/bin/env python3
"""NFL pregame snapshot collector. Never treat an in-game price as a pregame line."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

OUT = Path('closing-lines.json')
REPORT = Path('nfl-odds-diagnostics.json')
SCOREBOARD = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'
CORE = 'https://sports.core.api.espn.com/v2/sports/football/leagues/nfl'
ET = ZoneInfo('America/New_York')
NOW = datetime.now(timezone.utc)
CHECKPOINT_TOLERANCE_MIN = 10
errors, missing, captured = [], [], []


def request_json(url):
    req = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; NFLPregameOdds/1.0)',
        'Accept': 'application/json',
        'Referer': 'https://www.espn.com/'})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def american(v):
    if isinstance(v, dict):
        v = v.get('american', v.get('value', v.get('displayValue')))
    if isinstance(v, str):
        v = v.strip().upper().replace(',', '')
        if v in ('EVEN', 'EVENS', 'EV'):
            return 100
        v = v.replace('+', '')
    try:
        n = int(v)
        return n if 100 <= abs(n) <= 100000 else None
    except (ValueError, TypeError):
        return None


def implied(n):
    return -n / (100 - n) if n < 0 else 100 / (100 + n)


def line_from_core(items):
    """Only quote fields labeled current or top, and only before kickoff."""
    candidates = []
    for row in items:
        if not isinstance(row, dict):
            continue
        provider = row.get('provider') or {}
        name = str(provider.get('name') or 'Unknown')
        try:
            rank = int(provider.get('priority', 999))
        except (ValueError, TypeError):
            rank = 999
        away = row.get('awayTeamOdds') or {}
        home = row.get('homeTeamOdds') or {}
        for order, field in enumerate(('current', 'top')):
            a = away.get(field) if field == 'current' else away
            h = home.get(field) if field == 'current' else home
            if not isinstance(a, dict) or not isinstance(h, dict):
                continue
            am = american(a.get('moneyLine'))
            hm = american(h.get('moneyLine'))
            if am is not None and hm is not None:
                candidates.append((order, rank, name, am, hm, field))
    if not candidates:
        return None
    candidates.sort()
    _, _, provider, away_ml, home_ml, field = candidates[0]
    return away_ml, home_ml, f'ESPN Core {provider} {field}', 'core'


def line_from_scoreboard(competition):
    """Fallback is a second ESPN endpoint, not an independent bookmaker."""
    for odds in competition.get('odds') or []:
        if not isinstance(odds, dict):
            continue
        a = american((odds.get('awayTeamOdds') or {}).get('moneyLine'))
        h = american((odds.get('homeTeamOdds') or {}).get('moneyLine'))
        if a is not None and h is not None:
            name = (odds.get('provider') or {}).get('name') or 'Unknown'
            return a, h, f'ESPN scoreboard {name}', 'scoreboard'
    return None


def choose_checkpoint(game, key, candidate, minutes_to_kickoff, target):
    if abs(minutes_to_kickoff - target) > CHECKPOINT_TOLERANCE_MIN:
        return False
    prior = game['snapshots'].get(key)
    previous_diff = (
        abs(prior.get('minutes_before_kickoff', 9999) - target)
        if prior else float('inf')
    )
    current_diff = abs(minutes_to_kickoff - target)
    if prior and previous_diff < current_diff:
        return False
    if (
        prior
        and previous_diff == current_diff
        and prior.get('captured_at', '') >= candidate['captured_at']
    ):
        return False
    game['snapshots'][key] = dict(
        candidate,
        minutes_before_kickoff=round(minutes_to_kickoff, 2)
    )
    return True


def update_game(game, event, competition, kickoff, quote):
    away_name = next(
        (
            x.get('team', {}).get('displayName')
            for x in competition.get('competitors', [])
            if x.get('homeAway') == 'away'
        ), None
    )
    home_name = next(
        (
            x.get('team', {}).get('displayName')
            for x in competition.get('competitors', [])
            if x.get('homeAway') == 'home'
        ), None
    )
    if not away_name or not home_name:
        return False

    away_ml, home_ml, source, endpoint = quote
    source_url = (
        f'{CORE}/events/{event["id"]}/competitions/'
        f'{competition.get("id") or event["id"]}/odds'
        '?lang=en&region=us&limit=100'
        if endpoint == 'core' else SCOREBOARD
    )
    observed_at = datetime.now(timezone.utc)
    if observed_at >= kickoff:
        return False

    mins = (kickoff - observed_at).total_seconds() / 60
    candidate = dict(
        away_ml=away_ml,
        home_ml=home_ml,
        captured_at=observed_at.isoformat(),
        source=source,
        source_url=source_url
    )

    game.setdefault('snapshots', {})
    changed = False
    for key, target in (('t60', 60), ('t30', 30)):
        changed |= choose_checkpoint(game, key, candidate, mins, target)

    previous = game['snapshots'].get('latest_pregame')
    if not previous or previous.get('captured_at', '') < candidate['captured_at']:
        game['snapshots']['latest_pregame'] = dict(
            candidate,
            minutes_before_kickoff=round(mins, 2)
        )
        changed = True

    ap, hp = implied(away_ml), implied(home_ml)
    fair_away, fair_home = ap / (ap + hp), hp / (ap + hp)

    game.update(
        event_id=str(event['id']),
        date=kickoff.astimezone(ET).date().isoformat(),
        kickoff_utc=kickoff.isoformat(),
        away=away_name,
        home=home_name,
        away_ml=away_ml,
        home_ml=home_ml,
        away_fair_probability=round(fair_away, 6),
        home_fair_probability=round(fair_home, 6),
        pregame_favorite=(
            away_name if fair_away >= fair_home else home_name
        ),
        favorite_probability=round(max(fair_away, fair_home), 6),
        source=source + ' pregame snapshot (not confirmed close)',
        source_url=source_url,
        odds_timestamp=observed_at.isoformat(),
        captured_at=observed_at.isoformat(),
        line_status='latest_verified_pregame'
    )
    return changed


def main():
    if not OUT.exists():
        raise RuntimeError(
            'closing-lines.json is missing: refusing to reset existing history'
        )

    data = json.loads(OUT.read_text())
    if not isinstance(data.get('games'), dict):
        raise RuntimeError('closing-lines.json missing games dictionary')

    games = data['games']
    changed = False
    event_map = {}

    # Query the upcoming week dynamically.
    for delta in range(8):
        day = (
            NOW.astimezone(ET).date() + timedelta(days=delta)
        ).strftime('%Y%m%d')
        try:
            response = request_json(
                f'{SCOREBOARD}?dates={day}&limit=100'
            )
            for event in response.get('events') or []:
                event_map[str(event.get('id'))] = event
        except Exception as exc:
            errors.append(f'Scoreboard {day}: {exc}')

    for event in event_map.values():
        game_id = str(event.get('id'))
        competition = (event.get('competitions') or [{}])[0]

        try:
            kickoff = datetime.fromisoformat(
                event['date'].replace('Z', '+00:00')
            )
        except (KeyError, TypeError, ValueError):
            continue

        if not NOW < kickoff:
            continue

        game = games.get(game_id) or {}
        minutes_until_kickoff = (
            kickoff - NOW
        ).total_seconds() / 60
        previous = (
            game.get('snapshots') or {}
        ).get('latest_pregame')

        if minutes_until_kickoff > 120 and previous:
            try:
                last = datetime.fromisoformat(
                    previous['captured_at']
                )
                if NOW - last < timedelta(hours=6):
                    continue
            except (KeyError, ValueError, TypeError):
                pass

        core_url = (
            f'{CORE}/events/{game_id}/competitions/'
            f'{competition.get("id") or game_id}/odds'
            '?lang=en&region=us&limit=100'
        )

        try:
            payload = request_json(core_url)
            quote = line_from_core(
                payload.get('items') or []
            )
        except Exception as exc:
            errors.append(f'Core {game_id}: {exc}')
            quote = None

        if quote is None:
            quote = line_from_scoreboard(competition)

        if quote is None:
            minutes = (
                kickoff - NOW
            ).total_seconds() / 60
            missing.append(dict(
                event_id=game_id,
                kickoff=kickoff.isoformat(),
                minutes_before_kickoff=round(minutes, 1),
                has_saved_pregame=bool(
                    game.get('snapshots', {}).get('latest_pregame')
                    or (
                        game.get('away_ml')
                        and game.get('home_ml')
                    )
                )
            ))
            continue

        if datetime.now(timezone.utc) >= kickoff:
            continue

        if update_game(
            game, event, competition, kickoff, quote
        ):
            games[game_id] = game
            changed = True
            captured.append(game_id)

    if changed:
        data['updated_at'] = NOW.isoformat()
        OUT.write_text(
            json.dumps(data, indent=2, sort_keys=True) + '\n'
        )

    near_misses = [
        x for x in missing
        if x['minutes_before_kickoff'] <= 120
        and not x['has_saved_pregame']
    ]

    REPORT.write_text(
        json.dumps(dict(
            checked_at=NOW.isoformat(),
            checked_events=len(event_map),
            updated_events=len(set(captured)),
            missing_games=missing,
            missing_within_two_hours=near_misses,
            errors=errors
        ), indent=2) + '\n'
    )

    print(
        f'Checked {len(event_map)} events; '
        f'updated {len(set(captured))}; '
        f'missing near kickoff {len(near_misses)}'
    )

    for item in near_misses:
        print(
            '::warning::No verified pregame line for event',
            item['event_id']
        )
    for item in errors[:10]:
        print('::warning::', item)

    if not event_map and errors:
        raise RuntimeError(
            'No usable ESPN scoreboard data; see diagnostics artifact'
        )


if __name__ == '__main__':
    main()
