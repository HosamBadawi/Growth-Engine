"""Alembic environment. URL comes from engine.config; target metadata is our Base."""
from logging.config import fileConfig

from alembic import context

from db.models import Base
from db.session import get_engine

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False is REQUIRED, not cosmetic. Migrations run at
    # startup, after the engine's modules are imported, and fileConfig's default
    # (True) silently sets .disabled on every logger that already exists, so the
    # engine would run the rest of its life with no log output from those modules.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    from engine.config import get_settings

    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite needs batch mode for ALTER
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
