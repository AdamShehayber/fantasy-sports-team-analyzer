import os
import sys
import io
import json
import csv
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file, Response
from urllib.parse import urlparse
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import select, func
from .config import Config
from .extensions import csrf, limiter
from .models import Base, get_engine, Player, User, LeagueSettings, PlayerCatalog, SavedRoster, TradeReport, WatchlistItem
from .services.scoring import team_strength_v3, position_breakdown_v3, validate_lineup, can_add_starter, POSITION_LIMITS
from .services.stats_api import project_player, search_players, _fetch_sleeper_players
from .ai_helper import explain_trade_report
from .auth.routes import auth_bp, login_required
import plotly.graph_objects as go
import plotly.io as pio
import logging
from logging.handlers import RotatingFileHandler

# --- Trade Suggestions Helper ---
def _generate_trade_suggestions(before_players: list[dict], after_players: list[dict], scoring_type: str) -> dict:
    """Generate structured trade suggestions and improvements across seven categories.
    Returns a dict with keys for each category, each value is a list of suggestion strings.
    """
    # Compute team totals and breakdowns
    before_starter, before_bench = team_strength_v3(before_players, scoring_type)
    after_starter, after_bench = team_strength_v3(after_players, scoring_type)
    before_bd = position_breakdown_v3(before_players, scoring_type)
    after_bd = position_breakdown_v3(after_players, scoring_type)

    delta_total = round(after_starter - before_starter, 2)

    # Utility to score players
    from .services.scoring import calculate_player_score

    # Index players by position with scores
    def by_position(players):
        pos_map = {}
        for p in players:
            pos = (p.get('position') or 'UNK').upper()
            pos_map.setdefault(pos, []).append({
                'name': p.get('name'),
                'is_starter': bool(p.get('is_starter', True)),
                'score': float(calculate_player_score(p, scoring_type)),
            })
        # sort each list by score desc
        for pos, lst in pos_map.items():
            lst.sort(key=lambda x: x['score'], reverse=True)
        return pos_map

    before_pos = by_position(before_players)
    after_pos = by_position(after_players)

    suggestions = {
        'starter_optimization': [],
        'positional_improvement': [],
        'depth_and_bench': [],
        'positional_need_warnings': [],
        'unfavorable_trade_recos': [],
        'neutral_trade_suggestions': [],
        'final_summary': []
    }

    # 1) Starter Optimization: recommend swaps where a bench player outperforms lowest starter for a position
    for pos, players in after_pos.items():
        limit = POSITION_LIMITS.get(pos, 1)
        starters = [p for p in players if p['is_starter']][:limit]
        bench = [p for p in players if not p['is_starter']]
        if starters and bench:
            starters_sorted = sorted(starters, key=lambda x: x['score'])  # ascending to find weakest starter
            bench_sorted = sorted(bench, key=lambda x: x['score'], reverse=True)
            if bench_sorted[0]['score'] > starters_sorted[0]['score']:
                diff = bench_sorted[0]['score'] - starters_sorted[0]['score']
                suggestions['starter_optimization'].append(
                    f"Promote {bench_sorted[0]['name']} to {pos} starter (+{diff:.2f} vs current weakest {pos}).")

    # 2) Positional Improvement: compare before/after per position starter totals
    for pos in sorted(set(before_bd.keys()) | set(after_bd.keys())):
        b = before_bd.get(pos, {'starter': 0.0, 'bench': 0.0})
        a = after_bd.get(pos, {'starter': 0.0, 'bench': 0.0})
        d_starter = round(a['starter'] - b['starter'], 2)
        d_bench = round(a['bench'] - b['bench'], 2)
        if d_starter > 0:
            suggestions['positional_improvement'].append(f"{pos}: starter strength improved by +{d_starter:.2f}.")
        elif d_starter < 0:
            suggestions['positional_improvement'].append(f"{pos}: starter strength decreased by {d_starter:.2f}.")
        # Depth changes
        if d_bench != 0:
            sign = '+' if d_bench > 0 else ''
            suggestions['positional_improvement'].append(f"{pos}: bench depth change {sign}{d_bench:.2f}.")

    # 3) Depth & Bench Strategy: highlight thin positions and strong surplus
    for pos, bd in after_bd.items():
        bench_score = bd.get('bench', 0.0)
        starter_score = bd.get('starter', 0.0)
        # Thin bench threshold: relative to starter_score
        if starter_score > 0 and bench_score < (0.30 * starter_score):
            suggestions['depth_and_bench'].append(
                f"Depth is thin at {pos}. Consider waiver or 2-for-1 trade to add {pos} bench.")
        # Surplus: bench > starter
        if bench_score > starter_score:
            suggestions['depth_and_bench'].append(
                f"Bench surplus at {pos}. Package bench assets to upgrade another need.")

    # 4) Positional Need Warnings: lineup validation and missing starters
    validation = validate_lineup(after_players)
    for v in validation.get('violations', []):
        suggestions['positional_need_warnings'].append(
            f"Too many {v['position']} starters ({v['current']}/{v['limit']}). Move excess to bench.")
    # Warn if a position has zero starters after trade
    for pos, limit in POSITION_LIMITS.items():
        count = validation['starter_counts'].get(pos, 0)
        if limit > 0 and count == 0:
            suggestions['positional_need_warnings'].append(
                f"No {pos} starter set. Ensure at least {limit} {pos} in lineup.")

    # 5) Unfavorable trade recommendations
    if delta_total < 0:
        # Identify biggest decreases by position
        drops = []
        for pos in after_bd.keys():
            d_st = round(after_bd[pos]['starter'] - before_bd.get(pos, {'starter': 0.0})['starter'], 2)
            if d_st < 0:
                drops.append((pos, d_st))
        drops.sort(key=lambda x: x[1])
        for pos, d in drops[:3]:
            suggestions['unfavorable_trade_recos'].append(
                f"Recover {pos}: target a mid-tier upgrade (aim +{-d:.2f}).")
        suggestions['unfavorable_trade_recos'].append(
            "Consider counter-offering: swap a bench piece for a starter upgrade, or include a pick.")

    # 6) Neutral trade suggestions
    if delta_total == 0:
        suggestions['neutral_trade_suggestions'].append(
            "Seek marginal gains: prioritize positions with small positive bench deltas to convert into starters.")
        # Highlight a position with best bench improvement
        bench_improve = []
        for pos in after_bd.keys():
            d_b = round(after_bd[pos]['bench'] - before_bd.get(pos, {'bench': 0.0})['bench'], 2)
            if d_b > 0:
                bench_improve.append((pos, d_b))
        bench_improve.sort(key=lambda x: x[1], reverse=True)
        if bench_improve:
            pos, d = bench_improve[0]
            suggestions['neutral_trade_suggestions'].append(
                f"Best bench gain is at {pos} (+{d:.2f}). Explore promoting bench to starters.")

    # 7) Final Suggestion Summary
    decision = 'Accept' if delta_total > 0 else ('Neutral' if delta_total == 0 else 'Reject')
    suggestions['final_summary'].append(
        f"Decision: {decision} — starter Δ {delta_total:+.2f} (before {before_starter:.2f} → after {after_starter:.2f}).")

    return suggestions

def _resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, rel.replace('/', os.sep))

def create_app():
    return app
app = Flask(__name__, template_folder=_resource_path("frontend/templates"), static_folder=_resource_path("frontend/static"))
    
# Production logging to file (no console in packaged app)
try:
    log_dir = Config._app_data_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'app.log')
    handler = RotatingFileHandler(log_file, maxBytes=512000, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass
    
# Initialize secure configuration
# Initialize configuration
Config.init_app(app)

# Initialize extensions
csrf.init_app(app)
limiter.init_app(app)

# Register blueprints
app.register_blueprint(auth_bp)

engine = get_engine()
Base.metadata.create_all(engine)
# Session factory provides short-lived sessions per request/thread
SessionFactory = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    # Ensure the players table has a player_id column for robust matching
def ensure_player_id_column():
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info(players)").fetchall()
            cols = [r[1] for r in rows]
            if 'player_id' not in cols:
                conn.exec_driver_sql("ALTER TABLE players ADD COLUMN player_id VARCHAR(64)")
    except Exception:
        # Non-fatal: continue without migration; code treats player_id as optional
        pass
ensure_player_id_column()

def ensure_saved_roster_public_columns():
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info(saved_rosters)").fetchall()
            cols = [r[1] for r in rows]
            if 'is_public' not in cols:
                conn.exec_driver_sql("ALTER TABLE saved_rosters ADD COLUMN is_public BOOLEAN DEFAULT 0")
            if 'listed_at' not in cols:
                conn.exec_driver_sql("ALTER TABLE saved_rosters ADD COLUMN listed_at DATETIME")
    except Exception:
        # Non-fatal: continue without migration; listing is optional
        pass
ensure_saved_roster_public_columns()

def get_session(): #221
        return SessionFactory()

def is_logged_in():
        return 'user_id' in session and session.get('user_id') is not None

def get_live_context(): # 227
        """Season/week and toggle for live projections."""
        season = session.get('season')
        week = session.get('week')
        use_live = session.get('use_live_projections')
        if season is None:
            season = datetime.utcnow().year
            session['season'] = season
        if week is None:
            week = 1  # default; user can change
            session['week'] = week
        if use_live is None:
            use_live = app.config.get('USE_LIVE_PROJECTIONS_DEFAULT', True)
            session['use_live_projections'] = use_live
        return int(season), int(week), bool(use_live)

def ttl_minutes():
        return int(app.config.get('LIVE_CACHE_TTL_MINUTES', 360))

def minutes_ago(dt: datetime) -> int:
        return max(0, int((datetime.utcnow() - dt).total_seconds() // 60))

def strength_history_for_weeks(user_id: int, season: int, upto_week: int) -> list[dict]:
    """Approximate team strength progression by summing latest projections per week.
    Uses PlayerCatalog when available (by player_id or name/team/position),
    otherwise falls back to current roster projections.
    """
    history = []
    with get_session() as s:
        roster = s.scalars(select(Player).where(Player.user_id == user_id)).all()
        scoring_type_local = s.scalar(select(LeagueSettings.scoring_type).where(LeagueSettings.user_id == user_id)) or "PPR"
        for w in range(1, max(1, int(upto_week)) + 1):
            scoring_input = []
            for p in roster:
                live_row = None
                if p.player_id:
                    live_row = s.scalar(select(PlayerCatalog)
                                        .where(PlayerCatalog.player_id == p.player_id,
                                               PlayerCatalog.season == season,
                                               PlayerCatalog.week == w)
                                        .order_by(PlayerCatalog.updated_at.desc()))
                if not live_row:
                    live_row = s.scalar(select(PlayerCatalog)
                                        .where(PlayerCatalog.full_name == (p.name or ""),
                                               PlayerCatalog.team == ((p.team or "").upper()),
                                               PlayerCatalog.position == ((p.position or "").upper()),
                                               PlayerCatalog.season == season,
                                               PlayerCatalog.week == w)
                                        .order_by(PlayerCatalog.updated_at.desc()))
                proj = float(live_row.projection_points) if live_row else float(p.projection or 0.0)
                scoring_input.append(dict(name=p.name, position=p.position, team=p.team, projection=proj, is_starter=p.is_starter))
            starters, bench = team_strength_v3(scoring_input, scoring_type_local)
            history.append({"week": w, "starter_total": starters, "bench_total": bench})
    return history

def __setup_routes():
    @app.context_processor
    def inject_user():
        logged_in = is_logged_in()
        return dict(is_logged_in=logged_in, user_email=session.get('user_email'))

    @app.get("/")
    def home():
        # Remove About page from app navigation; redirect to a useful page.
        if is_logged_in():
            return redirect(url_for('dashboard'))
        return redirect(url_for('auth.login'))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        user_id = session['user_id']
        season, week, use_live = get_live_context()
        with get_session() as s:
            # Get user's players
            players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            player_dicts = []
            cache_ages = []
            for p in players:
                # Prefer matching by provider player_id when available
                live_row = None
                if p.player_id:
                    live_row = s.scalar(select(PlayerCatalog)
                                        .where(PlayerCatalog.player_id == p.player_id,
                                               PlayerCatalog.season == season,
                                               PlayerCatalog.week == week)
                                        .order_by(PlayerCatalog.updated_at.desc()))
                if not live_row:
                    # Fallback: match by normalized name/team/position
                    live_row = s.scalar(select(PlayerCatalog)
                                        .where(PlayerCatalog.full_name == (p.name or ""),
                                               PlayerCatalog.team == ((p.team or "").upper()),
                                               PlayerCatalog.position == ((p.position or "").upper()),
                                               PlayerCatalog.season == season,
                                               PlayerCatalog.week == week)
                                        .order_by(PlayerCatalog.updated_at.desc()))

                display_projection = p.projection
                projection_source = "manual"
                last_refresh_min = None
                if use_live and live_row:
                    age = minutes_ago(live_row.updated_at)
                    last_refresh_min = age
                    cache_ages.append(age)
                    # Use live only if within TTL; otherwise fall back and mark stale
                    if age <= ttl_minutes():
                        display_projection = float(live_row.projection_points or 0.0)
                        projection_source = "live"
                    else:
                        display_projection = p.projection
                        projection_source = "stale"

                player_dicts.append(dict(
                    id=p.id,
                    name=p.name,
                    position=p.position,
                    team=p.team,
                    projection=p.projection,
                    is_starter=p.is_starter,
                    display_projection=display_projection,
                    projection_source=projection_source,
                    last_refresh_min=last_refresh_min,
                ))
            
            # Get user's league settings
            league_settings = s.scalar(select(LeagueSettings).where(LeagueSettings.user_id == user_id))
            if not league_settings:
                # Create default league settings
                league_settings = LeagueSettings(user_id=user_id)
                s.add(league_settings)
                s.commit()
            # Fetch saved rosters and watchlist
            saved_rosters = s.scalars(select(SavedRoster).where(SavedRoster.user_id == user_id).order_by(SavedRoster.created_at.desc())).all()
            watchlist_items = s.scalars(select(WatchlistItem).where(WatchlistItem.user_id == user_id).order_by(WatchlistItem.created_at.desc())).all()
        # Effective projections for scoring use live if enabled
        scoring_input = [dict(
            name=p['name'], position=p['position'], team=p['team'],
            projection=(p['display_projection'] if use_live else p['projection']),
            is_starter=p['is_starter']
        ) for p in player_dicts]

        # Calculate separate starter and bench totals
        scoring_type = league_settings.scoring_type if league_settings else "PPR"
        starter_strength, bench_strength = team_strength_v3(scoring_input, scoring_type)
        breakdown = position_breakdown_v3(scoring_input, scoring_type)
        # Defer history computation to Charts page to speed up Dashboard
        strength_history = []

        # Simple suggestions: identify weakest starters and propose top free agents
        starters_only = [p for p in scoring_input if p['is_starter']]
        bench_only = [p for p in scoring_input if not p['is_starter']]
        weakest_starters = sorted(starters_only, key=lambda x: x['projection'])[:3]
        # Optional live suggestions (can be slow due to network). Toggle via env.
        add_targets = []
        if os.getenv('SUGGESTIONS_ENABLED', '0') == '1':
            try:
                for ws in weakest_starters:
                    candidates = search_players(query="", team=None, position=ws['position'], season=season, week=week, scoring_type=scoring_type, limit=5)
                    better = [c for c in candidates if float(c['projection_points']) > float(ws['projection'] or 0.0)]
                    if better:
                        add_targets.append(better[0])
            except Exception:
                add_targets = []
        bench_recommendations = sorted(starters_only, key=lambda x: x['projection'])[:3]
        
        # Validate lineup
        lineup_validation = validate_lineup(scoring_input)
        
        return render_template("dashboard.html", 
                             players=player_dicts, 
                             starter_strength=starter_strength,
                             bench_strength=bench_strength,
                             breakdown=breakdown, 
                             strength_history=strength_history,
                             add_targets=add_targets,
                             bench_recommendations=bench_recommendations,
                             saved_rosters=saved_rosters,
                             watchlist_items=watchlist_items,
                             league_settings=league_settings,
                             lineup_validation=lineup_validation,
                             position_limits=POSITION_LIMITS,
                             season=season,
                             week=week,
                             use_live=use_live,
                             live_cache_age=(min(cache_ages) if cache_ages else None))

    @app.get('/roster')
    @login_required
    def roster_page():
        user_id = session['user_id']
        season, week, use_live = get_live_context()
        with get_session() as s:
            players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            player_dicts = []
            cache_ages = []
            for p in players:
                live_row = None
                if p.player_id:
                    live_row = s.scalar(select(PlayerCatalog)
                                        .where(PlayerCatalog.player_id == p.player_id,
                                               PlayerCatalog.season == season,
                                               PlayerCatalog.week == week)
                                        .order_by(PlayerCatalog.updated_at.desc()))
                if not live_row:
                    live_row = s.scalar(select(PlayerCatalog)
                                        .where(PlayerCatalog.full_name == (p.name or ""),
                                               PlayerCatalog.team == ((p.team or "").upper()),
                                               PlayerCatalog.position == ((p.position or "").upper()),
                                               PlayerCatalog.season == season,
                                               PlayerCatalog.week == week)
                                        .order_by(PlayerCatalog.updated_at.desc()))

                display_projection = p.projection
                projection_source = "manual"
                last_refresh_min = None
                if use_live and live_row:
                    age = minutes_ago(live_row.updated_at)
                    last_refresh_min = age
                    cache_ages.append(age)
                    if age <= ttl_minutes():
                        display_projection = float(live_row.projection_points or 0.0)
                        projection_source = "live"
                    else:
                        display_projection = p.projection
                        projection_source = "stale"

                player_dicts.append(dict(
                    id=p.id,
                    name=p.name,
                    position=p.position,
                    team=p.team,
                    projection=p.projection,
                    is_starter=p.is_starter,
                    display_projection=display_projection,
                    projection_source=projection_source,
                    last_refresh_min=last_refresh_min,
                ))

            league_settings = s.scalar(select(LeagueSettings).where(LeagueSettings.user_id == user_id))
        scoring_input = [dict(name=p['name'], position=p['position'], team=p['team'], projection=(p['display_projection'] if use_live else p['projection']), is_starter=p['is_starter']) for p in player_dicts]
        lineup_validation = validate_lineup(scoring_input)
        return render_template('roster.html',
                               players=player_dicts,
                               lineup_validation=lineup_validation,
                               position_limits=POSITION_LIMITS,
                               season=season,
                               week=week,
                               use_live=use_live,
                               live_cache_age=(min(cache_ages) if cache_ages else None),
                               league_settings=league_settings)

    @app.get('/settings')
    @login_required
    def settings_page():
        user_id = session['user_id']
        season, week, use_live = get_live_context()
        cache_ages = []
        with get_session() as s:
            league_settings = s.scalar(select(LeagueSettings).where(LeagueSettings.user_id == user_id))
            players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            for p in players:
                live_row = s.scalar(select(PlayerCatalog)
                                    .where(PlayerCatalog.full_name == (p.name or ""),
                                           PlayerCatalog.team == ((p.team or "").upper()),
                                           PlayerCatalog.position == ((p.position or "").upper()),
                                           PlayerCatalog.season == season,
                                           PlayerCatalog.week == week)
                                    .order_by(PlayerCatalog.updated_at.desc()))
                if live_row:
                    cache_ages.append(minutes_ago(live_row.updated_at))
        return render_template('settings.html',
                               league_settings=league_settings,
                               season=season,
                               week=week,
                               use_live=use_live,
                               live_cache_age=(min(cache_ages) if cache_ages else None))

    @app.get('/trade')
    @login_required
    def trade_page():
        user_id = session['user_id']
        with get_session() as s:
            # My rosters
            my_rosters = s.scalars(
                select(SavedRoster)
                .where(SavedRoster.user_id == user_id)
                .order_by(SavedRoster.created_at.desc())
            ).all()

            # Public rosters from other users
            public_rows = s.execute(
                select(SavedRoster, User.email)
                .join(User, SavedRoster.user_id == User.id)
                .where(SavedRoster.is_public == True, SavedRoster.user_id != user_id)
                .order_by(SavedRoster.listed_at.desc().nullslast(), SavedRoster.created_at.desc())
            ).all()
            public_rosters = [
                {
                    'id': sr.id,
                    'name': sr.name,
                    'owner': (email or 'user')
                }
                for sr, email in public_rows
            ]

            # Fetch recent trade history for display (last 10) #532
            recent_reports = s.scalars(
                select(TradeReport)
                .where(TradeReport.user_id == user_id)
                .order_by(TradeReport.created_at.desc())
                .limit(10)
            ).all()

            # Prepare simple dicts for template (parse give/receive JSON)
            recent_trades = []
            for r in recent_reports:
                try:
                    give = json.loads(r.give_json) if r.give_json else []
                except Exception:
                    give = []
                try:
                    receive = json.loads(r.receive_json) if r.receive_json else []
                except Exception:
                    receive = []
                # Skip dummy or incomplete entries
                if (not give and not receive) or (r.other_roster or '').strip().upper() == 'UNKNOWN':
                    continue
                recent_trades.append({
                    'id': r.id,
                    'created_at': r.created_at,
                    'other_roster': r.other_roster,
                    'give': give,
                    'receive': receive,
                    'delta': r.delta,
                    'rationale': r.rationale,
                })
        latest_result = session.get('last_trade_result')
        return render_template('trade.html', my_rosters=my_rosters, public_rosters=public_rosters, recent_trades=recent_trades, latest_result=latest_result)
    


    @app.get('/trade/ai_explain/<int:report_id>')
    @login_required
    def trade_ai_explain(report_id):
        """Use OpenAI to explain a past trade report."""
        user_id = session['user_id']

        with get_session() as s:
            report = s.get(TradeReport, report_id)
            if not report or report.user_id != user_id:
                flash("Trade not found.", "error")
                return redirect(url_for('trade_page'))

            # Parse give/receive lists from JSON
            try:
                give = json.loads(report.give_json) if report.give_json else []
            except Exception:
                give = []

            try:
                receive = json.loads(report.receive_json) if report.receive_json else []
            except Exception:
                receive = []

            trade_dict = {
                "other_roster": report.other_roster,
                "give": give,
                "receive": receive,
                "before_strength": report.before_strength,
                "after_strength": report.after_strength,
                "delta": report.delta,
                "rationale": report.rationale,
            }

        # Call OpenAI; fail gracefully if something goes wrong
        try:
            ai_text = explain_trade_report(trade_dict)
        except Exception:
            ai_text = "AI explanation is not available right now."

        return render_template(
            "trade_ai_explain.html",
            trade=trade_dict,
            ai_text=ai_text,
        )


    @app.get('/watchlist')
    @login_required
    def watchlist_page():
        user_id = session['user_id']
        with get_session() as s:
            watchlist_items = s.scalars(select(WatchlistItem).where(WatchlistItem.user_id == user_id).order_by(WatchlistItem.created_at.desc())).all()
        return render_template('watchlist.html', watchlist_items=watchlist_items)

    @app.get('/my/players/json')
    @login_required
    def my_players_json():
        """Return current user's roster as JSON for picker rendering."""
        user_id = session['user_id']
        season, week, use_live = get_live_context()
        with get_session() as s:
            players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            # Use live projection if available and fresh; fallback to manual
            player_dicts = []
            for p in players:
                live_row = None
                if p.player_id:
                    live_row = s.scalar(select(PlayerCatalog)
                                        .where(PlayerCatalog.player_id == p.player_id,
                                               PlayerCatalog.season == season,
                                               PlayerCatalog.week == week)
                                        .order_by(PlayerCatalog.updated_at.desc()))
                if not live_row:
                    live_row = s.scalar(select(PlayerCatalog)
                                        .where(PlayerCatalog.full_name == (p.name or ""),
                                               PlayerCatalog.team == ((p.team or "").upper()),
                                               PlayerCatalog.position == ((p.position or "").upper()),
                                               PlayerCatalog.season == season,
                                               PlayerCatalog.week == week)
                                        .order_by(PlayerCatalog.updated_at.desc()))
                display_projection = p.projection
                if use_live and live_row and minutes_ago(live_row.updated_at) <= ttl_minutes():
                    display_projection = float(live_row.projection_points or 0.0)
                player_dicts.append(dict(
                    name=p.name,
                    position=p.position,
                    team=p.team,
                    projection=display_projection,
                    is_starter=p.is_starter
                ))
        return jsonify({"players": player_dicts})

    @app.get('/rosters/<int:roster_id>/players_json') #660
    @login_required
    def roster_players_json(roster_id: int):
        """Return saved roster players as JSON for picker rendering."""
        with get_session() as s:
            sr = s.scalar(select(SavedRoster).where(SavedRoster.id == roster_id))
            if not sr:
                return jsonify({"players": []}), 404
            try:
                players = json.loads(sr.players_json or '[]')
            except Exception:
                players = []
        # Normalize to expected shape
        normalized = [
            dict(
                name=(p.get('name') if isinstance(p, dict) else getattr(p, 'name', None)),
                position=(p.get('position') if isinstance(p, dict) else getattr(p, 'position', None)),
                team=(p.get('team') if isinstance(p, dict) else getattr(p, 'team', None)),
                projection=float((p.get('projection') if isinstance(p, dict) else getattr(p, 'projection', 0.0)) or 0.0),
                is_starter=bool((p.get('is_starter') if isinstance(p, dict) else getattr(p, 'is_starter', False)))
            ) for p in players
        ]
        return jsonify({"players": normalized})

    @app.get('/reports')
    @login_required
    def reports_page():
        user_id = session['user_id']
        with get_session() as s:
            saved_rosters_count = s.scalar(select(func.count(SavedRoster.id)).where(SavedRoster.user_id == user_id)) or 0
        return render_template('reports.html', saved_rosters_count=saved_rosters_count)

    @app.get('/charts') #692
    @login_required
    def charts_page():
        user_id = session['user_id']
        season, week, use_live = get_live_context()
        with get_session() as s:
            players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            player_dicts = []
            for p in players:
                live_row = s.scalar(select(PlayerCatalog)
                                    .where(PlayerCatalog.full_name == (p.name or ""),
                                           PlayerCatalog.team == ((p.team or "").upper()),
                                           PlayerCatalog.position == ((p.position or "").upper()),
                                           PlayerCatalog.season == season,
                                           PlayerCatalog.week == week)
                                    .order_by(PlayerCatalog.updated_at.desc()))
                display_projection = p.projection
                if use_live and live_row and minutes_ago(live_row.updated_at) <= ttl_minutes():
                    display_projection = float(live_row.projection_points or 0.0)
                player_dicts.append(dict(
                    name=p.name,
                    position=p.position,
                    team=p.team,
                    projection=display_projection,
                    is_starter=p.is_starter
                ))
            league_settings = s.scalar(select(LeagueSettings).where(LeagueSettings.user_id == user_id))
        scoring_type = league_settings.scoring_type if league_settings else "PPR"
        starter_strength, bench_strength = team_strength_v3(player_dicts, scoring_type)
        breakdown = position_breakdown_v3(player_dicts, scoring_type)
        strength_history = strength_history_for_weeks(user_id, season, week)
        return render_template('charts.html',
                               breakdown=breakdown,
                               strength_history=strength_history,
                               starter_strength=starter_strength,
                               bench_strength=bench_strength)

    @app.post("/players/add") #729
    @login_required
    def add_player():
        user_id = session['user_id']
        name = request.form.get("name","").strip()
        position = request.form.get("position","").strip().upper()
        team = request.form.get("team","").strip().upper()
        is_starter = request.form.get("is_starter", "true").lower() == "true"
        
        try:
            projection = float(request.form.get("projection","0") or 0)
        except ValueError:
            projection = 0.0
            
        if name and position:
            with get_session() as s:
                # Get current players to validate lineup
                current_players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
                current_player_dicts = [dict(
                    name=p.name, 
                    position=p.position, 
                    team=p.team, 
                    projection=p.projection,
                    is_starter=p.is_starter
                ) for p in current_players]
                
                # Check if we can add this player as a starter
                if is_starter and not can_add_starter(current_player_dicts, position):
                    limit = POSITION_LIMITS.get(position, 1)
                    flash(f'Cannot add {name} as starter - {position} position limit ({limit}) reached. Added to bench instead.', 'warning')
                    is_starter = False
                
                # Add the player
                s.add(Player(
                    user_id=user_id, 
                    name=name, 
                    position=position, 
                    team=team, 
                    projection=projection,
                    is_starter=is_starter
                ))
                s.commit()
                
            status = "starter" if is_starter else "bench"
            flash(f'Added {name} to your roster as {status}!', 'success')
        else:
            flash('Name and position are required.', 'error')
        return redirect_back("dashboard")

    @app.post("/stats/sync")
    @login_required
    def sync_stats():
        user_id = session['user_id']
        season, week, use_live = get_live_context()
        # Read-only session: determine scoring preset and collect player specs
        with get_session() as s:
            league_settings = s.scalar(select(LeagueSettings).where(LeagueSettings.user_id == user_id))
            scoring_type = league_settings.scoring_type if league_settings else "PPR"
            players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            player_specs = [(p.name or "", p.team, p.position) for p in players]

        # Fetch the external players catalog ONCE to avoid repeated network calls
        catalog = _fetch_sleeper_players()
        if not catalog:
            flash('Live data source unavailable. Please try again later.', 'error')
            return redirect_back('settings_page')

        updated = 0
        for name, team, position in player_specs:
            # Perform network call outside any DB transaction to minimize lock time
            try:
                results = search_players(query=name, team=team, position=position, season=season, week=week, scoring_type=scoring_type, limit=1, catalog=catalog)
            except Exception:
                results = []

            if results:
                r = results[0]
                # Short-lived write session to upsert PlayerCatalog
                with get_session() as ws:
                    with ws.begin():
                        existing = ws.scalar(select(PlayerCatalog)
                                             .where(PlayerCatalog.player_id == r['player_id'],
                                                    PlayerCatalog.season == season,
                                                    PlayerCatalog.week == week))
                        if existing:
                            existing.full_name = r['full_name']
                            existing.position = r['position']
                            existing.team = r['team']
                            existing.projection_points = float(r['projection_points'])
                            existing.updated_at = datetime.utcnow()
                            existing.source = r.get('source', 'live')
                        else:
                            ws.add(PlayerCatalog(
                                player_id=r['player_id'],
                                full_name=r['full_name'],
                                position=r['position'],
                                team=r['team'],
                                season=season,
                                week=week,
                                projection_points=float(r['projection_points']),
                                updated_at=datetime.utcnow(),
                                source=r.get('source', 'live')
                            ))
                updated += 1

        flash(f"Refreshed live projections for {updated} player(s).", 'success')
        return redirect_back('settings_page')

    @app.post('/live/settings')
    @login_required
    def update_live_settings():
        # Season/week/toggle controls
        season = int(request.form.get('season', datetime.utcnow().year))
        week = int(request.form.get('week', 1))
        use_live = request.form.get('use_live', 'off') == 'on'
        session['season'] = season
        session['week'] = week
        session['use_live_projections'] = use_live
        flash('Live settings updated.', 'info')
        return redirect_back('settings_page')

    @app.get('/api/search') #850
    @login_required
    @limiter.limit('30 per minute')
    def api_search():
        q = request.args.get('q', '')
        team = request.args.get('team')
        position = request.args.get('position')
        season, week, _ = get_live_context()
    
        with get_session() as s:
            league_settings = s.scalar(select(LeagueSettings).where(LeagueSettings.user_id == session['user_id']))
            scoring_type = league_settings.scoring_type if league_settings else 'PPR'
        try:
            results = search_players(q, team, position, season=season, week=week, scoring_type=scoring_type, limit=20)
        except Exception:
            results = []
        return jsonify({"results": results})

    @app.post('/players/add_from_search')
    @csrf.exempt
    @login_required
    def add_from_search():
        user_id = session['user_id']
        season, week, _ = get_live_context()
        player_id = request.form.get('player_id')
        full_name = request.form.get('full_name')
        position = (request.form.get('position') or '').upper()
        team = (request.form.get('team') or '').upper()
        target = request.form.get('target', 'starter')
        is_starter = target == 'starter'

        with get_session() as s:
            # Validate starter caps
            current_players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            current_player_dicts = [dict(
                name=p.name, position=p.position, team=p.team, projection=p.projection, is_starter=p.is_starter
            ) for p in current_players]

            if is_starter and not can_add_starter(current_player_dicts, position):
                limit = POSITION_LIMITS.get(position, 1)
                flash(f'Cannot add {full_name} as starter - {position} position limit ({limit}) reached. Try adding as Bench.', 'warning')
                return redirect_back('dashboard')

            # Add to roster
            s.add(Player(
                user_id=user_id,
                name=full_name,
                position=position,
                team=team,
                player_id=player_id,
                projection=0.0,  # keep manual field as fallback; display will use live
                is_starter=is_starter
            ))

            # Upsert into catalog so display can show live immediately
            if player_id:
                existing = s.scalar(select(PlayerCatalog)
                                    .where(PlayerCatalog.player_id == player_id,
                                           PlayerCatalog.season == season,
                                           PlayerCatalog.week == week))
                if existing:
                    existing.full_name = full_name
                    existing.position = position
                    existing.team = team
                    existing.updated_at = datetime.utcnow()
                    existing.source = 'live'
                else:
                    s.add(PlayerCatalog(
                        player_id=player_id,
                        full_name=full_name,
                        position=position,
                        team=team,
                        season=season,
                        week=week,
                        projection_points=float(project_player(full_name, position, team)),
                        updated_at=datetime.utcnow(),
                        source='live'
                    ))
            s.commit()

        flash(f'Added {full_name} from live search to your roster as {"starter" if is_starter else "bench"}.', 'success')
        return redirect_back('dashboard')

    @app.post("/players/<int:player_id>/toggle") #933
    @login_required
    def toggle_player_status(player_id):
        user_id = session['user_id']
        with get_session() as s:
            # Get the player
            player = s.scalar(select(Player).where(Player.id == player_id, Player.user_id == user_id))
            if not player:
                flash('Player not found.', 'error')
                return redirect_back("dashboard")
            
            # If switching from bench to starter, check limits
            if not player.is_starter:
                # Get current players to validate lineup
                current_players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
                current_player_dicts = [dict(
                    name=p.name, 
                    position=p.position, 
                    team=p.team, 
                    projection=p.projection,
                    is_starter=p.is_starter
                ) for p in current_players]
                
                if not can_add_starter(current_player_dicts, player.position):
                    limit = POSITION_LIMITS.get(player.position, 1)
                    flash(f'Cannot move {player.name} to starter - {player.position} position limit ({limit}) reached.', 'warning')
                    return redirect_back("dashboard")
            
            # Toggle the status
            player.is_starter = not player.is_starter
            s.commit()
            
            status = "starter" if player.is_starter else "bench"
            flash(f'Moved {player.name} to {status}.', 'success')
        
        return redirect_back("dashboard")

    @app.post("/players/clear")
    @login_required
    def clear_players():
        user_id = session['user_id']
        with get_session() as s:
            s.query(Player).filter(Player.user_id == user_id).delete()
            s.commit()
        flash('All players cleared from your roster.', 'info')
        return redirect_back("dashboard")

    @app.post('/watchlist/add')
    @csrf.exempt
    @login_required
    def add_watchlist():
        user_id = session['user_id']
        name = request.form.get('name') or ''
        position = (request.form.get('position') or '').upper()
        team = (request.form.get('team') or '').upper()
        player_id = request.form.get('player_id')
        if not name:
            flash('Select a player to add to watchlist.', 'warning')
            return redirect_back('watchlist_page')
        with get_session() as s:
            s.add(WatchlistItem(user_id=user_id, name=name, position=position, team=team, player_id=player_id))
            s.commit()
        flash(f'Added {name} to your watchlist.', 'success')
        return redirect_back('watchlist_page')

    @app.post('/rosters/save')
    @login_required
    def save_current_roster():
        user_id = session['user_id']
        name = request.form.get('roster_name') or f"Roster {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
        with get_session() as s:
            players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            data = [dict(name=p.name, position=p.position, team=p.team, projection=p.projection, is_starter=p.is_starter, player_id=p.player_id) for p in players]
            s.add(SavedRoster(user_id=user_id, name=name, players_json=json.dumps(data)))
            s.commit()
        flash(f'Saved current roster as "{name}".', 'success')
        return redirect_back('dashboard')

    @app.post('/trade/analyze') #1011
    @login_required
    def trade_analyze():
        """Simulate a trade: user proposes give/receive against a selected saved roster."""
        user_id = session['user_id']
        other_roster_id = request.form.get('other_roster_id')
        give_names = [n.strip() for n in (request.form.get('give') or '').split(',') if n.strip()]
        receive_names = [n.strip() for n in (request.form.get('receive') or '').split(',') if n.strip()]
        season, week, use_live = get_live_context()
        with get_session() as s:
            ls = s.scalar(select(LeagueSettings).where(LeagueSettings.user_id == user_id))
            scoring_type = ls.scoring_type if ls else 'PPR'
            # Load current roster
            my_players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            if not my_players:
                flash('Your roster is empty. Add players on the Dashboard before analyzing trades.', 'warning')
                return redirect_back('dashboard')
            # Load other roster
            if not other_roster_id:
                flash('Please select an "Against Roster" to analyze a trade.', 'warning')
                return redirect_back('trade_page')
            try:
                other_id_int = int(other_roster_id)
            except (TypeError, ValueError):
                flash('Invalid roster selection. Please choose a valid roster.', 'error')
                return redirect_back('trade_page')
            other = s.scalar(select(SavedRoster).where(SavedRoster.id == other_id_int)) if other_id_int else None
            if not other:
                flash('Selected roster not found. Please choose another roster.', 'error')
                return redirect_back('trade_page')
            other_players = json.loads(other.players_json) if other else []

            # Resolve effective projections (prefer live within TTL) for scoring
            def effective_projection(name, team, position, player_id, fallback_proj):
                live_row = None
                if player_id:
                    live_row = s.scalar(
                        select(PlayerCatalog)
                        .where(
                            PlayerCatalog.player_id == player_id,
                            PlayerCatalog.season == season,
                            PlayerCatalog.week == week,
                        )
                        .order_by(PlayerCatalog.updated_at.desc())
                    )
                if not live_row:
                    live_row = s.scalar(
                        select(PlayerCatalog)
                        .where(
                            PlayerCatalog.full_name == (name or ""),
                            PlayerCatalog.team == ((team or "").upper()),
                            PlayerCatalog.position == ((position or "").upper()),
                            PlayerCatalog.season == season,
                            PlayerCatalog.week == week,
                        )
                        .order_by(PlayerCatalog.updated_at.desc())
                    )
                proj = float(fallback_proj or 0.0)
                if use_live and live_row:
                    age = minutes_ago(live_row.updated_at)
                    if age <= ttl_minutes():
                        proj = float(live_row.projection_points or 0.0)
                return proj

            def to_input(items):
                return [dict(name=i['name'] if isinstance(i, dict) else i.name,
                             position=i['position'] if isinstance(i, dict) else i.position,
                             team=i['team'] if isinstance(i, dict) else i.team,
                             projection=(i['projection'] if isinstance(i, dict) else i.projection),
                             is_starter=(i['is_starter'] if isinstance(i, dict) else i.is_starter))
                        for i in items]

            # Build my_input using effective projections
            my_input = [
                dict(
                    name=p.name,
                    position=p.position,
                    team=p.team,
                    projection=effective_projection(p.name, p.team, p.position, p.player_id, p.projection),
                    is_starter=p.is_starter,
                )
                for p in my_players
            ]
            if not give_names and not receive_names:
                flash('Enter player names to Give or Receive (comma-separated) to simulate a trade.', 'warning')
                return redirect_back('trade_page')

            base_starters, base_bench = team_strength_v3(my_input, scoring_type)
            base_total = base_starters

            # Apply trade: remove 'give' from my roster, add 'receive' from other
            def remove_by_names(items, names):
                names_upper = {n.upper() for n in names}
                return [i for i in items if (i['name'] if isinstance(i, dict) else i.name).upper() not in names_upper]
            def pick_from_pool(pool, names):
                picked = []
                for n in names:
                    found = next((i for i in pool if (i['name'] if isinstance(i, dict) else i.name).upper() == n.upper()), None)
                    if found:
                        picked.append(found)
                return picked

            # Validate presence of specified players
            # Build other roster with effective projections for accurate scoring
            other_input = [
                dict(
                    name=(i.get('name') if isinstance(i, dict) else getattr(i, 'name', None)),
                    position=(i.get('position') if isinstance(i, dict) else getattr(i, 'position', None)),
                    team=(i.get('team') if isinstance(i, dict) else getattr(i, 'team', None)),
                    projection=effective_projection(
                        (i.get('name') if isinstance(i, dict) else getattr(i, 'name', None)),
                        (i.get('team') if isinstance(i, dict) else getattr(i, 'team', None)),
                        (i.get('position') if isinstance(i, dict) else getattr(i, 'position', None)),
                        (i.get('player_id') if isinstance(i, dict) else getattr(i, 'player_id', None)),
                        (i.get('projection') if isinstance(i, dict) else getattr(i, 'projection', 0.0)),
                    ),
                    is_starter=(i.get('is_starter') if isinstance(i, dict) else getattr(i, 'is_starter', False)),
                )
                for i in other_players
            ]

            my_names_upper = {p['name'].upper() for p in my_input}
            missing_give = [n for n in give_names if n.upper() not in my_names_upper]
            pool_names_upper = { i['name'].upper() for i in other_input }
            missing_receive = [n for n in receive_names if n.upper() not in pool_names_upper]
            if missing_give:
                flash(f"These 'Give' players were not found in your roster: {', '.join(missing_give)}.", 'error')
                return redirect_back('trade_page')
            if missing_receive:
                flash(f"These 'Receive' players were not found in the selected roster: {', '.join(missing_receive)}.", 'error')
                return redirect_back('trade_page')

            # Compose post-trade roster using effective projections
            my_post = remove_by_names(my_input, give_names) + pick_from_pool(other_input, receive_names)
            post_starters, post_bench = team_strength_v3(my_post, scoring_type)
            post_total = post_starters

            delta = round(post_total - base_total, 2)
            rationale = 'Accept' if delta > 0 else ('Neutral' if delta == 0 else 'Reject')
            detailed = f"Before: {base_total:.2f}, After: {post_total:.2f}, Δ: {delta:.2f} — {rationale}"

            s.add(TradeReport(
                user_id=user_id,
                other_roster=(other.name if other else 'Unknown'),
                give_json=json.dumps(give_names),
                receive_json=json.dumps(receive_names),
                before_strength=base_total,
                after_strength=post_total,
                delta=delta,
                rationale=detailed
            ))
            s.commit()

        # Generate suggestions and stash latest analysis in session for UI rendering
        try:
            suggestions = _generate_trade_suggestions(my_input, my_post, scoring_type)
        except Exception as e:
            app.logger.warning(f"Suggestion generation failed: {e}")
            suggestions = {k: [] for k in ['starter_optimization','positional_improvement','depth_and_bench','positional_need_warnings','unfavorable_trade_recos','neutral_trade_suggestions','final_summary']}

        session['last_trade_result'] = {
            'other_roster': (other.name if other else 'Unknown'),
            'give': give_names,
            'receive': receive_names,
            'before_strength': base_total,
            'after_strength': post_total,
            'delta': delta,
            'rationale': detailed,
            'suggestions': suggestions
        }

        flash(f'Trade analysis: {detailed}', 'info')
        return redirect_back('trade_page')

    @app.post('/rosters/<int:roster_id>/list')
    @login_required
    def list_saved_roster(roster_id: int):
        """Mark a saved roster as public so other users can trade against it."""
        user_id = session['user_id']
        with get_session() as s:
            sr = s.scalar(select(SavedRoster).where(SavedRoster.id == roster_id))
            if not sr or sr.user_id != user_id:
                flash('Cannot list: roster not found or not owned by you.', 'error')
                return redirect_back('trade_page')
            sr.is_public = True
            sr.listed_at = datetime.utcnow()
            s.commit()
        flash(f'Roster "{sr.name}" is now listed for trade.', 'success')
        return redirect_back('trade_page')

    @app.post('/rosters/<int:roster_id>/unlist')
    @login_required
    def unlist_saved_roster(roster_id: int):
        """Unlist a saved roster from public visibility."""
        user_id = session['user_id']
        with get_session() as s:
            sr = s.scalar(select(SavedRoster).where(SavedRoster.id == roster_id))
            if not sr or sr.user_id != user_id:
                flash('Cannot unlist: roster not found or not owned by you.', 'error')
                return redirect_back('trade_page')
            sr.is_public = False
            s.commit()
        flash(f'Roster "{sr.name}" has been unlisted.', 'info')
        return redirect_back('trade_page')

    @app.get('/export/csv/roster')
    @login_required
    def export_roster_csv():
        user_id = session['user_id']
        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(['Name','Position','Team','Projection','Starter'])
        with get_session() as s:
            players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            for p in players:
                writer.writerow([p.name, p.position, p.team, p.projection, 'Yes' if p.is_starter else 'No'])
        output = Response(si.getvalue(), mimetype='text/csv')
        output.headers['Content-Disposition'] = 'attachment; filename=roster.csv'
        return output

    @app.get('/reports/download')
    @login_required
    def download_report():
        fmt = request.args.get('format', 'pdf')
        user_id = session['user_id']
        season, week, use_live = get_live_context()
        with get_session() as s:
            ls = s.scalar(select(LeagueSettings).where(LeagueSettings.user_id == user_id))
            scoring_type = ls.scoring_type if ls else 'PPR'
            players = s.scalars(select(Player).where(Player.user_id == user_id)).all()
            # Use display projections consistent with dashboard (prefer live within TTL)
            roster = []
            for p in players:
                display_projection = p.projection
                try:
                    live_row = s.scalar(
                        select(PlayerCatalog)
                        .where(
                            PlayerCatalog.full_name == (p.name or ""),
                            PlayerCatalog.team == ((p.team or "").upper()),
                            PlayerCatalog.position == ((p.position or "").upper()),
                            PlayerCatalog.season == season,
                            PlayerCatalog.week == week,
                        )
                        .order_by(PlayerCatalog.updated_at.desc())
                    )
                    if use_live and live_row and minutes_ago(live_row.updated_at) <= ttl_minutes():
                        display_projection = float(live_row.projection_points or 0.0)
                except Exception:
                    # Fall back to stored projection if live lookup fails
                    pass
                roster.append(dict(
                    name=p.name,
                    position=p.position,
                    team=p.team,
                    projection=display_projection,
                    is_starter=p.is_starter
                ))
            starters, bench = team_strength_v3(roster, scoring_type)
            breakdown = position_breakdown_v3(roster, scoring_type)

        if fmt == 'csv':
            si = io.StringIO()
            writer = csv.writer(si)
            writer.writerow(['User','Season','Week','Scoring','Starter Total','Bench Total'])
            writer.writerow([session.get('user_email','user'), season, week, scoring_type, starters, bench])
            writer.writerow([])
            writer.writerow(['Name','Position','Team','Projection','Starter'])
            for r in roster:
                writer.writerow([r['name'], r['position'], r['team'], r['projection'], 'Yes' if r['is_starter'] else 'No'])
            output = Response(si.getvalue(), mimetype='text/csv')
            output.headers['Content-Disposition'] = 'attachment; filename=team_report.csv'
            return output
        else:
            # Build a PDF with aligned roster columns; include a pie chart PNG via plotly+kaleido when available
            from reportlab.lib.pagesizes import letter
            from reportlab.pdfgen import canvas
            tmp = io.BytesIO()
            c = canvas.Canvas(tmp, pagesize=letter)
            c.setTitle('Fantasy Team Report')
            c.drawString(40, 750, 'Fantasy Sports Team Analyzer')
            c.drawString(40, 735, f"User: {session.get('user_email','user')}  Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ")
            c.drawString(40, 720, f"Season {season}, Week {week}, Scoring {scoring_type}")
            c.drawString(40, 700, f"Starters Total: {starters:.2f}   Bench Total: {bench:.2f}")
            y = 680
            c.drawString(40, y, 'Roster:')
            y -= 18
            # Column headers and positions
            x_name, x_pos, x_team, x_proj, x_role = 50, 250, 300, 350, 420
            c.setFont('Helvetica-Bold', 10)
            c.drawString(x_name, y, 'Name')
            c.drawString(x_pos, y, 'Pos')
            c.drawString(x_team, y, 'Team')
            c.drawString(x_proj, y, 'Proj')
            c.drawString(x_role, y, 'Role')
            c.setFont('Helvetica', 10)
            y -= 12
            c.line(50, y, 520, y)
            y -= 10
            for r in roster[:25]:
                proj = float(r.get('projection') or 0.0)
                c.drawString(x_name, y, (r['name'] or ''))
                c.drawString(x_pos, y, (r['position'] or ''))
                c.drawString(x_team, y, (r['team'] or ''))
                c.drawRightString(x_proj + 30, y, f"{proj:.2f}")
                c.drawString(x_role, y, ('Starter' if r['is_starter'] else 'Bench'))
                y -= 14
                if y < 120:
                    # New page if reaching bottom
                    c.showPage()
                    c.setTitle('Fantasy Team Report')
                    c.drawString(40, 750, 'Fantasy Sports Team Analyzer')
                    c.drawString(40, 735, f"User: {session.get('user_email','user')}  Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ")
                    c.drawString(40, 720, f"Season {season}, Week {week}, Scoring {scoring_type}")
                    c.drawString(40, 700, 'Roster (cont.):')
                    y = 680

            # Position breakdown chart: embed PNG if available; otherwise omit silently
            try:
                labels = list(breakdown.keys())
                values = [float((breakdown[k].starter or 0)) + float((breakdown[k].bench or 0)) for k in labels]
                fig = go.Figure(data=[go.Pie(labels=labels, values=values, hole=.3)])
                fig.update_layout(template='plotly_dark')
                png_bytes = pio.to_image(fig, format='png', width=600, height=400, scale=1)
                from reportlab.lib.utils import ImageReader
                img = ImageReader(io.BytesIO(png_bytes))
                c.drawImage(img, 300, 400, width=250, height=180)
            except Exception:
                # Optional static PNG fallback; if not present, do nothing
                try:
                    static_png = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'frontend', 'static', 'chart_fallback.png'))
                    if os.path.exists(static_png):
                        c.drawImage(static_png, 300, 400, width=250, height=180)
                except Exception:
                    pass

            c.showPage()
            c.save()
            tmp.seek(0)
            return send_file(tmp, as_attachment=True, download_name='team_report.pdf', mimetype='application/pdf')

    @app.post("/league/settings")
    @login_required
    def update_league_settings():
        user_id = session['user_id']
        scoring_type = request.form.get("scoring_type", "PPR")
        teams = int(request.form.get("teams", 12))
        qb_slots = int(request.form.get("qb_slots", 1))
        rb_slots = int(request.form.get("rb_slots", 2))
        wr_slots = int(request.form.get("wr_slots", 2))
        te_slots = int(request.form.get("te_slots", 1))
        flex_slots = int(request.form.get("flex_slots", 1))
        k_slots = int(request.form.get("k_slots", 1))
        d_st_slots = int(request.form.get("d_st_slots", 1))
        
        with get_session() as s:
            league_settings = s.scalar(select(LeagueSettings).where(LeagueSettings.user_id == user_id))
            if league_settings:
                league_settings.scoring_type = scoring_type
                league_settings.teams = teams
                league_settings.qb_slots = qb_slots
                league_settings.rb_slots = rb_slots
                league_settings.wr_slots = wr_slots
                league_settings.te_slots = te_slots
                league_settings.flex_slots = flex_slots
                league_settings.k_slots = k_slots
                league_settings.d_st_slots = d_st_slots
            else:
                league_settings = LeagueSettings(
                    user_id=user_id,
                    scoring_type=scoring_type,
                    teams=teams,
                    qb_slots=qb_slots,
                    rb_slots=rb_slots,
                    wr_slots=wr_slots,
                    te_slots=te_slots,
                    flex_slots=flex_slots,
                    k_slots=k_slots,
                    d_st_slots=d_st_slots
                )
                s.add(league_settings)
            s.commit()
        
        flash('League settings updated successfully!', 'success')
        return redirect(url_for("dashboard"))

__setup_routes()

## Script entrypoint removed; use Flask CLI for dev server

# Helper: redirect back to the referring page when safe.
def redirect_back(default_endpoint: str = 'dashboard'):
    ref = request.headers.get('Referer')
    if ref:
        u = urlparse(ref)
        # Only allow redirects back to our own host
        if u.netloc == request.host and u.path:
            return redirect(ref)
    return redirect(url_for(default_endpoint))
