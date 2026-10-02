# Generated manually for append-only manifest enforcement.
# ORM 层（信号 + 冻结管理器）之外，再由数据库触发器兜底：
# 即便绕过应用直接执行 SQL，清单行 / 收件锚点也无法 UPDATE/DELETE，
# 版本表除 status 状态机迁移外内容列无法变更。
from django.db import migrations


# SQLite 每条语句单独执行（驱动 execute() 不支持多语句）
_SQLITE_FORWARD = [
    """
    CREATE TRIGGER cd_attachment_no_update
    BEFORE UPDATE ON cd_intake_attachment
    BEGIN
        SELECT RAISE(ABORT, 'cd_intake_attachment is append-only');
    END
    """,
    """
    CREATE TRIGGER cd_attachment_no_delete
    BEFORE DELETE ON cd_intake_attachment
    BEGIN
        SELECT RAISE(ABORT, 'cd_intake_attachment is append-only');
    END
    """,
    """
    CREATE TRIGGER cd_packet_no_update
    BEFORE UPDATE ON cd_intake_packet
    BEGIN
        SELECT RAISE(ABORT, 'cd_intake_packet is append-only');
    END
    """,
    """
    CREATE TRIGGER cd_packet_no_delete
    BEFORE DELETE ON cd_intake_packet
    BEGIN
        SELECT RAISE(ABORT, 'cd_intake_packet is append-only');
    END
    """,
    """
    CREATE TRIGGER cd_version_freeze_content
    BEFORE UPDATE ON cd_intake_version
    WHEN NEW.packet_id IS NOT OLD.packet_id
        OR NEW.version_no IS NOT OLD.version_no
        OR NEW.change_type IS NOT OLD.change_type
        OR NEW.submitted_by_id IS NOT OLD.submitted_by_id
        OR NEW.submitted_by_name IS NOT OLD.submitted_by_name
        OR NEW.remark IS NOT OLD.remark
        OR NEW.manifest_hash IS NOT OLD.manifest_hash
    BEGIN
        SELECT RAISE(ABORT, 'cd_intake_version content is immutable (status transitions only)');
    END
    """,
    """
    CREATE TRIGGER cd_version_no_delete
    BEFORE DELETE ON cd_intake_version
    BEGIN
        SELECT RAISE(ABORT, 'cd_intake_version is append-only');
    END
    """,
]

_SQLITE_REVERSE = [
    'DROP TRIGGER IF EXISTS cd_attachment_no_update',
    'DROP TRIGGER IF EXISTS cd_attachment_no_delete',
    'DROP TRIGGER IF EXISTS cd_packet_no_update',
    'DROP TRIGGER IF EXISTS cd_packet_no_delete',
    'DROP TRIGGER IF EXISTS cd_version_freeze_content',
    'DROP TRIGGER IF EXISTS cd_version_no_delete',
]

_POSTGRES_FORWARD = """
CREATE OR REPLACE FUNCTION cd_raise_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;
CREATE OR REPLACE FUNCTION cd_raise_version_content()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.packet_id IS DISTINCT FROM OLD.packet_id
        OR NEW.version_no IS DISTINCT FROM OLD.version_no
        OR NEW.change_type IS DISTINCT FROM OLD.change_type
        OR NEW.submitted_by_id IS DISTINCT FROM OLD.submitted_by_id
        OR NEW.submitted_by_name IS DISTINCT FROM OLD.submitted_by_name
        OR NEW.remark IS DISTINCT FROM OLD.remark
        OR NEW.manifest_hash IS DISTINCT FROM OLD.manifest_hash
    THEN
        RAISE EXCEPTION 'cd_intake_version content is immutable (status transitions only)';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER cd_attachment_immutable
    BEFORE UPDATE OR DELETE ON cd_intake_attachment
    FOR EACH ROW EXECUTE FUNCTION cd_raise_append_only();
CREATE TRIGGER cd_packet_immutable
    BEFORE UPDATE OR DELETE ON cd_intake_packet
    FOR EACH ROW EXECUTE FUNCTION cd_raise_append_only();
CREATE TRIGGER cd_version_freeze_content
    BEFORE UPDATE ON cd_intake_version
    FOR EACH ROW EXECUTE FUNCTION cd_raise_version_content();
CREATE TRIGGER cd_version_no_delete
    BEFORE DELETE ON cd_intake_version
    FOR EACH ROW EXECUTE FUNCTION cd_raise_append_only();
"""

_POSTGRES_REVERSE = """
DROP TRIGGER IF EXISTS cd_attachment_immutable ON cd_intake_attachment;
DROP TRIGGER IF EXISTS cd_packet_immutable ON cd_intake_packet;
DROP TRIGGER IF EXISTS cd_version_freeze_content ON cd_intake_version;
DROP TRIGGER IF EXISTS cd_version_no_delete ON cd_intake_version;
DROP FUNCTION IF EXISTS cd_raise_version_content();
DROP FUNCTION IF EXISTS cd_raise_append_only();
"""


def install_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'sqlite':
        for statement in _SQLITE_FORWARD:
            schema_editor.execute(statement)
    elif vendor == 'postgresql':
        # psycopg 允许一次执行多条语句
        schema_editor.execute(_POSTGRES_FORWARD)


def drop_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'sqlite':
        for statement in _SQLITE_REVERSE:
            schema_editor.execute(statement)
    elif vendor == 'postgresql':
        schema_editor.execute(_POSTGRES_REVERSE)


class Migration(migrations.Migration):

    dependencies = [
        ('custody', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(install_triggers, drop_triggers),
    ]
