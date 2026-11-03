from logging.config import fileConfig

from alembic import context
from flask import current_app

config = context.config

if config.config_file_name is not None:
    import os
    if os.path.exists(config.config_file_name):
        fileConfig(config.config_file_name)

target_metadata = current_app.extensions["migrate"].db.metadata


def get_engine():
    db = current_app.extensions["migrate"].db
    try:
        return db.engine
    except (AttributeError, TypeError):
        return db.get_engine()


def get_engine_url():
    return get_engine().url.render_as_string(
        hide_password=False
    ).replace("%", "%%")


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        url = get_engine_url()

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()