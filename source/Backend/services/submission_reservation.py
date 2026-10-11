"""Schema-owned reservation guard, installed by migration, not at runtime entry."""
from sqlalchemy import text


def reservation_guard_present(connection):
    if connection.dialect.name=='sqlite':
        return connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN ('saved_execution_update','saved_execution_delete')"))==2
    if connection.dialect.name!='postgresql': return False
    schema=connection.get_execution_options().get('schema_translate_map',{}).get(None)
    if schema is None: schema=connection.scalar(text('SELECT current_schema()'))
    return bool(connection.scalar(text("""SELECT EXISTS (SELECT 1 FROM pg_trigger t
        JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema AND c.relname='saved_strategies'
        AND t.tgname='saved_execution_reservation' AND t.tgenabled='O')"""),{'schema':schema}))


def install_reservation_guard(connection):
    if connection.dialect.name=='sqlite':
        for action in ('UPDATE','DELETE'):
            connection.execute(text(f"""CREATE TRIGGER IF NOT EXISTS saved_execution_{action.lower()}
                BEFORE {action} ON saved_strategies
                WHEN EXISTS (SELECT 1 FROM trade_submission_attempts a
                    WHERE a.owner_id=OLD.owner_id
                    AND json_extract(a.strategy_identity,'$.strategy_id')=OLD.strategy_id
                    AND json_extract(a.send_intent,'$.state')='UNRESOLVED')
                BEGIN SELECT RAISE(ABORT,'STRATEGY_EXECUTION_UNRESOLVED'); END"""))
        return
    if connection.dialect.name!='postgresql':
        raise RuntimeError('RECOVERY_POSTGRES_REQUIRED')
    schema=connection.get_execution_options().get('schema_translate_map',{}).get(None)
    if schema is None: schema=connection.scalar(text('SELECT current_schema()'))
    quoted=connection.dialect.identifier_preparer.quote_schema(schema)
    connection.execute(text(f"""CREATE OR REPLACE FUNCTION {quoted}.guard_saved_execution()
        RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
          IF current_setting('transaction_isolation') <> 'read committed' THEN
            RAISE EXCEPTION 'STRATEGY_WRITE_ISOLATION_UNSUPPORTED';
          END IF;
          IF EXISTS(SELECT 1 FROM {quoted}.trade_submission_attempts a
             WHERE a.owner_id=OLD.owner_id
             AND a.strategy_identity->>'strategy_id'=OLD.strategy_id
             AND a.send_intent->>'state'='UNRESOLVED') THEN
            RAISE EXCEPTION 'STRATEGY_EXECUTION_UNRESOLVED';
          END IF;
          IF TG_OP='DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$"""))
    connection.execute(text(f'DROP TRIGGER IF EXISTS saved_execution_reservation ON {quoted}.saved_strategies'))
    connection.execute(text(f'''CREATE TRIGGER saved_execution_reservation
        BEFORE UPDATE OR DELETE ON {quoted}.saved_strategies
        FOR EACH ROW EXECUTE FUNCTION {quoted}.guard_saved_execution()'''))
