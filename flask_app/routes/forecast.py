"""M2b: Sales Forecast Dashboard routes."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
from datetime import date, timedelta, datetime
from database import get_all_stores

from db.forecast import (
    generate_forecasts,
    get_forecasts,
    get_consolidated_forecasts,
    set_forecast_override,
    associate_real_sales,
    get_accuracy_data,
    get_meteo_config,
    update_meteo_config,
    calibrate_meteo_multipliers,
    get_epoch_label,
    get_homolog_sales,
    get_past_sales,
)

forecast_bp = Blueprint('forecast', __name__)

WEEKDAY_PT = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']


def _get_venda_stores():
    """Return active stores that are of type 'loja'."""
    try:
        stores = get_all_stores()
        return [s for s in stores if s.get('is_active')]
    except Exception:
        return []


def _store_names():
    return [s['name'] for s in _get_venda_stores()]


# ---------------------------------------------------------------------------
# Index — 7-day forecast dashboard
# ---------------------------------------------------------------------------

@forecast_bp.route('/')
@perm_required('acesso_gestor')
def index():
    loja = request.args.get('loja') or None
    stores = _get_venda_stores()
    store_names = [s['name'] for s in stores]

    if loja and loja not in store_names:
        loja = None

    today = date.today()
    from_date = today + timedelta(days=1)
    to_date = today + timedelta(days=7)

    # Generate fresh forecasts (force=True when ?recalcular=1 is passed)
    import logging as _log
    force = request.args.get('recalcular') == '1'
    if loja:
        try:
            # generate_forecasts returns in-memory dicts with all signal fields
            # (factor_momentum, ly_anchor_eur, effective_value).  Use them directly
            # instead of re-reading from DB so the UI always shows the live signals.
            generated = generate_forecasts(loja, horizon_days=7, force=force)
        except Exception as e:
            _log.getLogger(__name__).exception("generate_forecasts(%s) failed", loja)
            flash(f'Erro ao gerar previsões para {loja}: {e}', 'warning')
            generated = None
    else:
        generated = None
        for sname in store_names:
            try:
                generate_forecasts(sname, horizon_days=7, force=force)
            except Exception as e:
                _log.getLogger(__name__).exception("generate_forecasts(%s) failed", sname)
                flash(f'Erro ao gerar previsões para {sname}: {e}', 'warning')
    if force:
        flash('Previsões recalculadas com dados actualizados.', 'success')

    if loja:
        if generated is not None:
            # Filter to the display window and enrich weekday labels
            forecasts = [f for f in generated if from_date <= f['data'] <= to_date]
            homolog = get_homolog_sales(loja, from_date, to_date)
            for f in forecasts:
                ly_date = date(f['data'].year - 1, f['data'].month, f['data'].day)
                f['homolog_eur'] = homolog.get(ly_date)
        else:
            forecasts = get_forecasts(loja=loja, from_date=from_date, to_date=to_date)
            homolog = get_homolog_sales(loja, from_date, to_date)
            for f in forecasts:
                ly_date = date(f['data'].year - 1, f['data'].month, f['data'].day)
                f['homolog_eur'] = homolog.get(ly_date)
        consolidated = None
    else:
        forecasts = []
        for sname in store_names:
            store_fc = get_forecasts(loja=sname, from_date=from_date, to_date=to_date)
            forecasts.extend(store_fc)
        consolidated = get_consolidated_forecasts(from_date=from_date, to_date=to_date)

    # Epoch and YoY for header
    epoch = get_epoch_label(today)
    avg_yoy = None
    if forecasts:
        yoys = [float(f['factor_yoy']) for f in forecasts if f.get('factor_yoy')]
        avg_yoy = round(sum(yoys) / len(yoys) * 100 - 100, 1) if yoys else None

    # Weekday labels
    for f in forecasts:
        f['weekday_label'] = WEEKDAY_PT[f['data'].weekday()]
    if consolidated:
        for c in consolidated:
            c['weekday_label'] = WEEKDAY_PT[c['data'].weekday()]

    return render_template(
        'forecast/index.html',
        stores=stores,
        selected_loja=loja,
        forecasts=forecasts,
        consolidated=consolidated,
        epoch=epoch,
        avg_yoy=avg_yoy,
        today=today,
        from_date=from_date,
        to_date=to_date,
    )


# ---------------------------------------------------------------------------
# Model explanation panel for a single day
# ---------------------------------------------------------------------------

@forecast_bp.route('/explicacao')
@perm_required('acesso_gestor')
def explicacao():
    loja = request.args.get('loja', '')
    data_str = request.args.get('data', '')
    stores = _get_venda_stores()
    store_names = [s['name'] for s in stores]

    if not loja or loja not in store_names:
        flash('Loja inválida.', 'error')
        return redirect(url_for('forecast.index'))

    try:
        target_date = date.fromisoformat(data_str)
    except (ValueError, TypeError):
        flash('Data inválida.', 'error')
        return redirect(url_for('forecast.index'))

    # Get or generate forecast for that day
    rows = get_forecasts(loja=loja, from_date=target_date, to_date=target_date)
    fc = rows[0] if rows else None

    # Load meteo config for this store
    meteo_config = get_meteo_config(loja)

    # Load last 8 same-weekday historical sales
    from db.connection import db_connection as _dbc
    wd = target_date.weekday()
    history_samples = []
    with _dbc() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT data, valor_euros FROM vendas
            WHERE loja = %s AND EXTRACT(DOW FROM data) = %s AND data < %s
            ORDER BY data DESC LIMIT 8
        """, (loja, (wd + 1) % 7 if wd < 6 else 0, target_date))
        # Note: EXTRACT(DOW ...) uses 0=Sunday, weekday() uses 0=Monday
        # Use a broader query and filter in Python:
    with _dbc() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT data, valor_euros FROM vendas
            WHERE loja = %s AND data < %s
            ORDER BY data DESC LIMIT 112
        """, (loja, target_date))
        all_hist = [(r[0], float(r[1])) for r in cursor.fetchall()]

    same_wd = [(d, v) for d, v in all_hist if d.weekday() == wd][:8]
    same_wd.reverse()  # oldest first

    for i, (d, v) in enumerate(same_wd):
        weight = i + 1
        history_samples.append({
            'data': d,
            'valor': v,
            'peso': weight,
            'weekday_label': WEEKDAY_PT[d.weekday()],
        })

    epoch = get_epoch_label(target_date) if fc else None
    weekday_label = WEEKDAY_PT[target_date.weekday()]

    return render_template(
        'forecast/explicacao.html',
        loja=loja,
        stores=stores,
        target_date=target_date,
        weekday_label=weekday_label,
        fc=fc,
        history_samples=history_samples,
        meteo_config=meteo_config,
        epoch=epoch,
    )


# ---------------------------------------------------------------------------
# Manual override
# ---------------------------------------------------------------------------

@forecast_bp.route('/override', methods=['POST'])
@perm_required('acesso_gestor')
def override():
    loja = request.form.get('loja', '').strip()
    data_str = request.form.get('data', '').strip()
    valor_str = request.form.get('override_eur', '').strip()
    motivo = request.form.get('motivo', '').strip()
    clear = request.form.get('clear') == '1'

    store_names = _store_names()
    if not loja or loja not in store_names:
        flash('Loja inválida.', 'error')
        return redirect(url_for('forecast.index'))

    try:
        target_date = date.fromisoformat(data_str)
    except (ValueError, TypeError):
        flash('Data inválida.', 'error')
        return redirect(url_for('forecast.index'))

    if clear:
        set_forecast_override(loja, target_date, None, None)
        flash('Override removido.', 'success')
    else:
        try:
            valor = float(valor_str.replace(',', '.'))
        except (ValueError, TypeError):
            flash('Valor inválido.', 'error')
            return redirect(url_for('forecast.explicacao', loja=loja, data=data_str))
        set_forecast_override(loja, target_date, valor, motivo or None)
        flash(f'Override de {valor:.2f} € registado para {loja} em {target_date.strftime("%d/%m/%Y")}.', 'success')

    return redirect(url_for('forecast.explicacao', loja=loja, data=data_str))


# ---------------------------------------------------------------------------
# Accuracy dashboard
# ---------------------------------------------------------------------------

@forecast_bp.route('/precisao')
@perm_required('acesso_gestor')
def precisao():
    loja = request.args.get('loja') or None
    stores = _get_venda_stores()
    store_names = [s['name'] for s in stores]

    if loja and loja not in store_names:
        loja = None

    # Associate real sales first
    for sname in store_names:
        try:
            associate_real_sales(sname)
        except Exception:
            pass

    accuracy = get_accuracy_data(loja=loja, weeks=12)

    # Trend: list of {year, week, mape, n} — convert to display string
    trend = accuracy.get('error_trend', [])
    for t in trend:
        # approximate week start date
        try:
            from datetime import date as _dt
            import datetime as _dt_mod
            jan4 = _dt(t['year'], 1, 4)
            week_start = jan4 + timedelta(weeks=t['week'] - 1) - timedelta(days=jan4.weekday())
            t['label'] = week_start.strftime('%d/%m')
        except Exception:
            t['label'] = f"S{t['week']}"

    for w in accuracy.get('worst_days', []):
        if isinstance(w.get('data'), date):
            w['data_fmt'] = w['data'].strftime('%d/%m/%Y')
            w['weekday_label'] = WEEKDAY_PT[w['data'].weekday()]

    return render_template(
        'forecast/precisao.html',
        stores=stores,
        selected_loja=loja,
        accuracy=accuracy,
        trend=trend,
    )


# ---------------------------------------------------------------------------
# Meteo config
# ---------------------------------------------------------------------------

@forecast_bp.route('/meteo-config', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def meteo_config():
    stores = _get_venda_stores()
    store_names = [s['name'] for s in stores]
    loja = request.args.get('loja') or (store_names[0] if store_names else None)

    if request.method == 'POST':
        loja = request.form.get('loja', '').strip()
        score_min = int(request.form.get('score_min', 0))
        score_max = int(request.form.get('score_max', 29))
        try:
            mult = float(request.form.get('multiplicador', '1').replace(',', '.'))
            update_meteo_config(loja, score_min, score_max, mult)
            flash('Multiplicador meteorológico actualizado.', 'success')
        except (ValueError, TypeError):
            flash('Valor inválido.', 'error')
        return redirect(url_for('forecast.meteo_config', loja=loja))

    config = get_meteo_config(loja) if loja else []
    return render_template(
        'forecast/meteo_config.html',
        stores=stores,
        selected_loja=loja,
        config=config,
    )


# ---------------------------------------------------------------------------
# Auto-calibration endpoint
# ---------------------------------------------------------------------------

@forecast_bp.route('/meteo-config/calibrar', methods=['POST'])
@perm_required('acesso_gestor')
def meteo_config_calibrar():
    loja = request.form.get('loja', '').strip()
    days_str = request.form.get('days', '90')

    store_names = _store_names()
    if not loja or loja not in store_names:
        flash('Loja inválida.', 'error')
        return redirect(url_for('forecast.meteo_config'))

    try:
        days = max(90, min(180, int(days_str)))
    except (ValueError, TypeError):
        days = 90

    result = calibrate_meteo_multipliers(loja, days=days)

    if 'error' in result:
        flash(f'Calibração falhou: {result["error"]}', 'warning')
    else:
        flash(
            f'Multiplicadores recalibrados com {result["matched_days"]} dias de dados '
            f'(janela de {result["days_analysed"]} dias).',
            'success',
        )

    return redirect(url_for('forecast.meteo_config', loja=loja))


# ---------------------------------------------------------------------------
# Modelo de Previsão — 21-day history + 21-day forecast table
# ---------------------------------------------------------------------------

@forecast_bp.route('/modelo')
@perm_required('acesso_gestor')
def modelo():
    loja = request.args.get('loja') or None
    stores = _get_venda_stores()
    store_names = [s['name'] for s in stores]

    if loja and loja not in store_names:
        loja = None

    today = date.today()
    force = request.args.get('recalcular') == '1'
    past_from = today - timedelta(days=21)
    past_to = today - timedelta(days=1)
    # Forecast window: today (day 0) through today+20 = 21 forecast rows total
    # generate_forecasts produces today+1 … today+horizon; today is handled separately.
    fc_from = today
    fc_to = today + timedelta(days=20)  # 21 rows: today..today+20

    import logging as _log

    # --- Past sales (21 rows: today-21..today-1) ---
    past_rows = get_past_sales(loja, past_from, past_to)

    # Enrich past rows with recorded meteo condition from meteo_cache
    from db.connection import db_connection as _dbc

    def _meteo_label(score):
        if score is None:
            return None
        if score < 30:
            return 'Mau tempo'
        if score < 60:
            return 'Tempo incerto'
        if score < 85:
            return 'Bom tempo'
        return 'Excelente'

    past_meteo_scores: dict = {}
    try:
        with _dbc() as _conn:
            _cur = _conn.cursor()
            _cur.execute("SAVEPOINT sp_pm")
            _cur.execute(
                "SELECT data, score FROM meteo_cache WHERE data >= %s AND data <= %s",
                (past_from, past_to)
            )
            for _r in _cur.fetchall():
                past_meteo_scores[_r[0]] = int(_r[1]) if _r[1] is not None else None
            _cur.execute("RELEASE SAVEPOINT sp_pm")
    except Exception:
        pass
    for r in past_rows:
        score = past_meteo_scores.get(r['data'])
        r['score_meteo'] = score
        r['condicao_meteo'] = _meteo_label(score)
        r['tipo'] = 'real'

    # --- Forecast rows ---
    # generate_forecasts always produces today+1 … today+horizon.
    # We pass horizon_days=20 to get today+1..today+20 (20 rows).
    # For today itself we read from sales_forecasts (stored by yesterday's run).
    _today_dg = ['semana','semana','semana','semana','sexta','fds','fds'][today.weekday()]
    _today_dt = 'semana' if today.weekday() < 4 else ('sexta' if today.weekday() == 4 else 'fds')

    def _make_today_fallback(ref_rows, store=None):
        """Build a minimal today forecast row from same-weekday reference rows."""
        same_wd = [f for f in ref_rows if f['data'].weekday() == today.weekday()]
        base_val = float(same_wd[0].get('previsao_eur') or same_wd[0].get('effective_value') or 0) if same_wd else (
            float(ref_rows[0].get('previsao_eur') or ref_rows[0].get('effective_value') or 0) if ref_rows else 0.0
        )
        return {
            'loja': store or 'Todas',
            'data': today,
            'previsao_eur': round(base_val, 2),
            'banda_min': round(base_val * 0.85, 2),
            'banda_max': round(base_val * 1.15, 2),
            'score_meteo': None,
            'condicao_meteo': None,
            'factor_yoy': None,
            'override_manual': None,
            'override_motivo': None,
            'venda_real': None,
            'epoch': get_epoch_label(today),
            'day_type': _today_dt,
            'day_group': _today_dg,
            'effective_value': round(base_val, 2),
            'factor_momentum': None,
            'signal_n': None,
        }

    if loja:
        try:
            generated = generate_forecasts(loja, horizon_days=20, force=force)
            fc_rows = [f for f in generated if f['data'] <= fc_to]
        except Exception as e:
            _log.getLogger(__name__).exception("modelo: generate_forecasts(%s)", loja)
            flash(f'Erro ao gerar previsões: {e}', 'warning')
            fc_rows = get_forecasts(loja=loja, from_date=today + timedelta(days=1), to_date=fc_to)
        # Prepend today's stored forecast row if available (1 row for today)
        today_rows = get_forecasts(loja=loja, from_date=today, to_date=today)
        if today_rows:
            fc_rows = today_rows + fc_rows
        else:
            fc_rows = [_make_today_fallback(fc_rows, store=loja)] + fc_rows
    else:
        for sname in store_names:
            try:
                generate_forecasts(sname, horizon_days=20, force=force)
            except Exception as e:
                _log.getLogger(__name__).exception("modelo: generate_forecasts(%s)", sname)
        fc_rows = get_consolidated_forecasts(from_date=fc_from, to_date=fc_to)
        # Ensure today row is present in consolidated view
        if not any(f['data'] == today for f in fc_rows):
            fc_rows = [_make_today_fallback(fc_rows, store=None)] + fc_rows

    if force:
        flash('Previsões recalculadas com dados actualizados.', 'success')

    for f in fc_rows:
        f['tipo'] = 'previsao'
        f.setdefault('weekday_label', WEEKDAY_PT[f['data'].weekday()])
        f.setdefault('day_group', ['semana','semana','semana','semana','sexta','fds','fds'][f['data'].weekday()])

    # --- Compute weekly totals ---
    def _week_key(d):
        iso = d.isocalendar()
        return (iso[0], iso[1])

    # Combine all rows for the table
    all_rows = past_rows + fc_rows
    all_rows.sort(key=lambda r: r['data'])

    # Week subtotals: aggregate by ISO week
    week_real: dict = {}
    week_fc: dict = {}
    for r in past_rows:
        k = _week_key(r['data'])
        week_real[k] = week_real.get(k, 0.0) + r['valor']
    for f in fc_rows:
        k = _week_key(f['data'])
        val = float(f.get('previsao_eur') or f.get('effective_value') or 0)
        week_fc[k] = week_fc.get(k, 0.0) + val

    total_real = sum(r['valor'] for r in past_rows)
    total_fc = sum(float(f.get('previsao_eur') or f.get('effective_value') or 0) for f in fc_rows)

    return render_template(
        'forecast/modelo.html',
        stores=stores,
        selected_loja=loja,
        today=today,
        past_rows=past_rows,
        fc_rows=fc_rows,
        all_rows=all_rows,
        total_real=total_real,
        total_fc=total_fc,
        week_real=week_real,
        week_fc=week_fc,
    )
