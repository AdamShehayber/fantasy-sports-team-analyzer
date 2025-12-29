from sqlalchemy.orm import DeclarativeBase, mapped_column, Mapped, relationship
from sqlalchemy import String, Integer, Float, create_engine, ForeignKey, DateTime, Boolean, event, Text
from datetime import datetime
import os

def _app_data_dir():
    base = (
        os.environ.get('LOCALAPPDATA')
        or os.environ.get('APPDATA')
        or os.path.expanduser('~')
    )
    path = os.path.join(base, 'FantasyAnalyzer')
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path

# Database location (override with FANTASY_DB_PATH)
DB_PATH = os.environ.get('FANTASY_DB_PATH') or os.path.join(_app_data_dir(), "fantasy.sqlite")

class Base(DeclarativeBase):
    pass

def get_engine(): #25
    # Ensure the database file exists in the project directory
    # Configure SQLite for desktop app concurrency: WAL mode and busy timeout
    engine = create_engine(
        f"sqlite+pysqlite:///{DB_PATH}",
        echo=False,
        future=True,
        pool_pre_ping=True,
        connect_args={
            "check_same_thread": False,  # allow connections to be used across threads
            "timeout": 15,               # wait for locks before failing
        },
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, connection_record):
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
        except Exception:
            # Safe to ignore if PRAGMAs cannot be set; app remains functional
            pass

    return engine

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    remember_token: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    login_count: Mapped[int] = mapped_column(Integer, default=0)
    
    # Relationships
    players: Mapped[list["Player"]] = relationship("Player", back_populates="user", cascade="all, delete-orphan")
    league_settings: Mapped["LeagueSettings"] = relationship("LeagueSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")
    session_logs: Mapped[list["SessionLog"]] = relationship("SessionLog", back_populates="user", cascade="all, delete-orphan")

class LeagueSettings(Base):
    __tablename__ = "league_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    scoring_type: Mapped[str] = mapped_column(String(20), default="PPR")  # Standard, PPR, Half-PPR
    teams: Mapped[int] = mapped_column(Integer, default=12)
    qb_slots: Mapped[int] = mapped_column(Integer, default=1)
    rb_slots: Mapped[int] = mapped_column(Integer, default=2)
    wr_slots: Mapped[int] = mapped_column(Integer, default=2)
    te_slots: Mapped[int] = mapped_column(Integer, default=1)
    flex_slots: Mapped[int] = mapped_column(Integer, default=1)
    k_slots: Mapped[int] = mapped_column(Integer, default=1)
    d_st_slots: Mapped[int] = mapped_column(Integer, default=1)
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="league_settings")

class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[str] = mapped_column(String(10))
    team: Mapped[str] = mapped_column(String(10))
    # Stable provider id (e.g., Sleeper player_id) for robust matching
    player_id: Mapped[str] = mapped_column(String(64), nullable=True)
    projection: Mapped[float] = mapped_column(Float, default=0.0)
    is_starter: Mapped[bool] = mapped_column(Boolean, default=True)  # True for starter, False for bench
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="players")

class SessionLog(Base):
    __tablename__ = "session_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    login_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    logout_time: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    session_duration: Mapped[int] = mapped_column(Integer, nullable=True)  # in seconds
    ip_address: Mapped[str] = mapped_column(String(45), nullable=True)  # IPv6 compatible
    user_agent: Mapped[str] = mapped_column(String(500), nullable=True)
    remember_me_used: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="session_logs")

class PlayerCatalog(Base):
    __tablename__ = "player_catalog"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Provider stable id (e.g., Sleeper player_id)
    player_id: Mapped[str] = mapped_column(String(64), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    position: Mapped[str] = mapped_column(String(10), nullable=True)
    team: Mapped[str] = mapped_column(String(10), nullable=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=True)
    projection_points: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # 'live' or 'manual'

class SavedRoster(Base):
    __tablename__ = "saved_rosters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    players_json: Mapped[str] = mapped_column(Text, nullable=False)  # serialized list of players
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Public listing for multi-user trade marketplace
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    listed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # Relationships
    user: Mapped["User"] = relationship("User")

class TradeReport(Base):
    __tablename__ = "trade_reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    other_roster: Mapped[str] = mapped_column(String(120), nullable=False)
    give_json: Mapped[str] = mapped_column(Text, nullable=False)
    receive_json: Mapped[str] = mapped_column(Text, nullable=False)
    before_strength: Mapped[float] = mapped_column(Float, default=0.0)
    after_strength: Mapped[float] = mapped_column(Float, default=0.0)
    delta: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Relationships
    user: Mapped["User"] = relationship("User")

class WatchlistItem(Base):    #156
    __tablename__ = "watchlist"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    position: Mapped[str] = mapped_column(String(10), nullable=True)
    team: Mapped[str] = mapped_column(String(10), nullable=True)
    player_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Relationships
    user: Mapped["User"] = relationship("User")
