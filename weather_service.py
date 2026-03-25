import os
import logging
import math
import requests
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_HISTORICAL_URL = "https://archive-api.open-meteo.com/v1/archive"
IPMA_FORECAST_URL = "https://api.ipma.pt/open-data/forecast/meteorology/cities/daily/{location_id}.json"
IPMA_LOCATIONS_URL = "https://api.ipma.pt/open-data/distrits-islands.json"

ACCUWEATHER_BASE = "https://dataservice.accuweather.com"
WU_BASE = "https://api.weather.com/v2"

IPMA_CITY_IDS = {
    "Porto": 1131200,
    "Matosinhos": 1131200,
    "Bolhão": 1131200,
    "Lisboa": 1110600,
    "Braga": 1030300,
    "Coimbra": 1060300,
    "Aveiro": 1010500,
    "Faro": 1080500,
    "Setúbal": 1151200,
    "Évora": 1070500,
    "Viseu": 1180500,
}

IPMA_CITY_COORDS = {
    1131200: (41.1496, -8.6109),
    1110600: (38.7223, -9.1393),
    1030300: (41.5454, -8.4265),
    1060300: (40.2033, -8.4103),
    1010500: (40.6443, -8.6455),
    1080500: (37.0194, -7.9304),
    1151200: (38.5244, -8.8882),
    1070500: (38.5711, -7.9090),
    1180500: (40.6566, -7.9122),
    1020500: (41.8197, -8.4270),
    1040200: (40.2088, -8.4192),
    1050200: (39.4667, -8.0000),
    1071100: (38.5244, -7.9090),
    1160200: (39.7444, -8.8072),
    1182300: (40.6566, -7.6140),
}

WMO_CODE_MAP = {
    0: "Limpo",
    1: "Principalmente limpo", 2: "Parcialmente nublado", 3: "Nublado",
    45: "Nevoeiro", 48: "Nevoeiro com geada",
    51: "Chuvisco ligeiro", 53: "Chuvisco moderado", 55: "Chuvisco intenso",
    61: "Chuva ligeira", 63: "Chuva moderada", 65: "Chuva intensa",
    71: "Neve ligeira", 73: "Neve moderada", 75: "Neve intensa",
    80: "Aguaceiros ligeiros", 81: "Aguaceiros moderados", 82: "Aguaceiros violentos",
    95: "Trovoada", 96: "Trovoada c/ granizo ligeiro", 99: "Trovoada c/ granizo",
}

IPMA_WEATHER_TYPE = {
    "1": "Sol", "2": "Sol e nuvens", "3": "Ligeiramente nublado",
    "4": "Nublado", "5": "Muitas nuvens", "6": "Nublado com chuva fraca",
    "7": "Nublado com chuva", "8": "Nublado com chuva forte",
    "9": "Chuva e vento", "10": "Chuva forte e vento",
    "11": "Aguaceiros", "12": "Aguaceiros e vento",
    "13": "Chuva e trovoada", "14": "Neve", "15": "Neve e chuva",
    "16": "Geada", "17": "Nevoeiro", "18": "Nevoeiro", "19": "Sol",
    "20": "Sol", "21": "Sol", "22": "Sol", "23": "Sol",
    "24": "Sol", "25": "Sol", "26": "Sol", "27": "Sol",
    "28": "Sol", "29": "Sol", "30": "Sol",
}


def wmo_to_condition(code: int) -> str:
    return WMO_CODE_MAP.get(code, f"Código {code}")


def calc_weather_score(temp_max: Optional[float], precip_mm: Optional[float],
                       wind_kmh: Optional[float], uv_index: Optional[float]) -> float:
    score = 50.0
    if temp_max is not None:
        if temp_max >= 28:
            score += 25
        elif temp_max >= 22:
            score += 15
        elif temp_max >= 18:
            score += 5
        elif temp_max < 12:
            score -= 20
        elif temp_max < 16:
            score -= 10

    if precip_mm is not None:
        if precip_mm == 0:
            score += 10
        elif precip_mm < 2:
            score += 3
        elif precip_mm < 10:
            score -= 10
        else:
            score -= 20

    if wind_kmh is not None:
        if wind_kmh > 50:
            score -= 15
        elif wind_kmh > 30:
            score -= 7

    if uv_index is not None:
        if uv_index >= 6:
            score += 5
        elif uv_index >= 3:
            score += 2

    return max(0.0, min(100.0, round(score, 1)))


def fetch_openmeteo_forecast(lat: float, lon: float, days: int = 16) -> list[dict]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max,weathercode,uv_index_max",
        "forecast_days": min(days, 16),
        "timezone": "Europe/Lisbon",
    }
    try:
        r = requests.get(OPENMETEO_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        results = []
        for i, d in enumerate(dates):
            temp_max = daily.get("temperature_2m_max", [None] * len(dates))[i]
            temp_min = daily.get("temperature_2m_min", [None] * len(dates))[i]
            precip = daily.get("precipitation_sum", [None] * len(dates))[i]
            wind = daily.get("windspeed_10m_max", [None] * len(dates))[i]
            wcode = daily.get("weathercode", [None] * len(dates))[i]
            uv = daily.get("uv_index_max", [None] * len(dates))[i]
            score = calc_weather_score(temp_max, precip, wind, uv)
            results.append({
                "fonte": "open-meteo",
                "data": d,
                "temperatura_max": temp_max,
                "temperatura_min": temp_min,
                "precipitacao_mm": precip,
                "vento_kmh": wind,
                "condicao": wmo_to_condition(wcode) if wcode is not None else None,
                "uv_index": uv,
                "score": score,
                "raw": {"weathercode": wcode},
            })
        return results
    except Exception as e:
        logger.warning("Open-Meteo forecast error: %s", e)
        return []


def fetch_openmeteo_historical(lat: float, lon: float, start: date, end: date) -> list[dict]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max,weathercode,uv_index_max",
        "timezone": "Europe/Lisbon",
    }
    try:
        r = requests.get(OPENMETEO_HISTORICAL_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        results = []
        for i, d in enumerate(dates):
            temp_max = daily.get("temperature_2m_max", [None] * len(dates))[i]
            temp_min = daily.get("temperature_2m_min", [None] * len(dates))[i]
            precip = daily.get("precipitation_sum", [None] * len(dates))[i]
            wind = daily.get("windspeed_10m_max", [None] * len(dates))[i]
            wcode = daily.get("weathercode", [None] * len(dates))[i]
            uv = daily.get("uv_index_max", [None] * len(dates))[i]
            score = calc_weather_score(temp_max, precip, wind, uv)
            results.append({
                "fonte": "open-meteo",
                "data": d,
                "temperatura_max": temp_max,
                "temperatura_min": temp_min,
                "precipitacao_mm": precip,
                "vento_kmh": wind,
                "condicao": wmo_to_condition(wcode) if wcode is not None else None,
                "uv_index": uv,
                "score": score,
                "raw": {"weathercode": wcode},
            })
        return results
    except Exception as e:
        logger.warning("Open-Meteo historical error: %s", e)
        return []


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _ipma_city_id(store_name: str, address: str = None, lat: float = None, lon: float = None) -> Optional[int]:
    if lat is not None and lon is not None:
        best_id = None
        best_dist = float("inf")
        for city_id, (clat, clon) in IPMA_CITY_COORDS.items():
            d = _haversine_km(lat, lon, clat, clon)
            if d < best_dist:
                best_dist = d
                best_id = city_id
        if best_id is not None:
            logger.debug("IPMA nearest city for (%.4f, %.4f): %s (%.1f km)", lat, lon, best_id, best_dist)
            return best_id

    cid = IPMA_CITY_IDS.get(store_name)
    if cid:
        return cid
    if address:
        for city, cid in IPMA_CITY_IDS.items():
            if city.lower() in address.lower():
                return cid
    return IPMA_CITY_IDS.get("Porto")


def fetch_ipma_forecast(store_name: str, address: str = None, lat: float = None, lon: float = None) -> list[dict]:
    city_id = _ipma_city_id(store_name, address, lat, lon)
    if not city_id:
        return []
    url = IPMA_FORECAST_URL.format(location_id=city_id)
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        forecasts = data.get("data", [])
        results = []
        for f in forecasts:
            temp_max = f.get("tMax")
            temp_min = f.get("tMin")
            precip_prob = f.get("precipitaProb")
            wind_dir = f.get("ddVento")
            wind_class = f.get("classeVentoMed")
            weather_type = f.get("idWeatherType")
            condicao = IPMA_WEATHER_TYPE.get(str(weather_type), f"Tipo {weather_type}")
            try:
                temp_max_f = float(temp_max) if temp_max is not None else None
                temp_min_f = float(temp_min) if temp_min is not None else None
            except (ValueError, TypeError):
                temp_max_f = temp_min_f = None

            precip_mm = None
            if precip_prob is not None:
                try:
                    precip_mm = float(precip_prob) * 0.1
                except (ValueError, TypeError):
                    pass

            wind_kmh = None
            if wind_class is not None:
                try:
                    wind_kmh = float(wind_class) * 5.5
                except (ValueError, TypeError):
                    pass

            score = calc_weather_score(temp_max_f, precip_mm, wind_kmh, None)
            results.append({
                "fonte": "ipma",
                "data": f.get("forecastDate"),
                "temperatura_max": temp_max_f,
                "temperatura_min": temp_min_f,
                "precipitacao_mm": precip_mm,
                "vento_kmh": wind_kmh,
                "condicao": condicao,
                "uv_index": None,
                "score": score,
                "raw": {"idWeatherType": weather_type, "precipitaProb": precip_prob, "ddVento": wind_dir},
            })
        return results
    except Exception as e:
        logger.warning("IPMA forecast error for %s: %s", store_name, e)
        return []


def fetch_accuweather_forecast(lat: float, lon: float) -> list[dict]:
    api_key = os.environ.get("ACCUWEATHER_API_KEY", "")
    if not api_key:
        logger.info("AccuWeather API key not configured, skipping.")
        return []
    try:
        loc_url = f"{ACCUWEATHER_BASE}/locations/v1/cities/geoposition/search"
        r = requests.get(loc_url, params={"apikey": api_key, "q": f"{lat},{lon}"}, timeout=10)
        r.raise_for_status()
        location_key = r.json().get("Key")
        if not location_key:
            return []

        fc_url = f"{ACCUWEATHER_BASE}/forecasts/v1/daily/5day/{location_key}"
        r2 = requests.get(fc_url, params={"apikey": api_key, "metric": "true", "details": "true"}, timeout=10)
        r2.raise_for_status()
        fc_data = r2.json()
        results = []
        for day in fc_data.get("DailyForecasts", []):
            epoch = day.get("EpochDate")
            day_date = date.fromtimestamp(epoch).isoformat() if epoch else None
            temp_max = day.get("Temperature", {}).get("Maximum", {}).get("Value")
            temp_min = day.get("Temperature", {}).get("Minimum", {}).get("Value")
            realfeel_max = day.get("RealFeelTemperature", {}).get("Maximum", {}).get("Value")
            precip_mm = day.get("Day", {}).get("TotalLiquid", {}).get("Value")
            wind_kmh = day.get("Day", {}).get("Wind", {}).get("Speed", {}).get("Value")
            condicao = day.get("Day", {}).get("LongPhrase", "")
            score = calc_weather_score(temp_max, precip_mm, wind_kmh, None)
            results.append({
                "fonte": "accuweather",
                "data": day_date,
                "temperatura_max": temp_max,
                "temperatura_min": temp_min,
                "precipitacao_mm": precip_mm,
                "vento_kmh": wind_kmh,
                "condicao": condicao,
                "uv_index": None,
                "score": score,
                "raw": {"realfeel_max": realfeel_max},
            })
        return results
    except Exception as e:
        logger.warning("AccuWeather forecast error: %s", e)
        return []


def fetch_wu_forecast(lat: float, lon: float) -> list[dict]:
    """Fetch 5-day forecast from Weather.com v3 geocode forecast endpoint (WU free tier)."""
    api_key = os.environ.get("WU_API_KEY", "")
    if not api_key:
        logger.info("Weather Underground API key not configured, skipping.")
        return []
    try:
        url = "https://api.weather.com/v3/wx/forecast/daily/5day"
        params = {
            "geocode": f"{lat},{lon}",
            "format": "json",
            "units": "m",
            "language": "pt-PT",
            "apiKey": api_key,
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = []
        valid_dates = data.get("validTimeUtc", [])
        temp_max_list = data.get("temperatureMax", [])
        temp_min_list = data.get("temperatureMin", [])
        precip_list = data.get("qpf", [])
        wind_list = data.get("windSpeedAverage", [])
        narrative = data.get("narrative", [])
        uv_list = data.get("uvIndex", [])

        for i, epoch in enumerate(valid_dates):
            if epoch is None:
                continue
            day_date = date.fromtimestamp(epoch).isoformat()
            temp_max = temp_max_list[i] if i < len(temp_max_list) else None
            temp_min = temp_min_list[i] if i < len(temp_min_list) else None
            precip_mm = precip_list[i] if i < len(precip_list) else None
            wind_kmh = wind_list[i] if i < len(wind_list) else None
            condicao = narrative[i] if i < len(narrative) else None
            uv = uv_list[i] if i < len(uv_list) else None
            score = calc_weather_score(temp_max, precip_mm, wind_kmh, uv)
            results.append({
                "fonte": "weather-underground",
                "data": day_date,
                "temperatura_max": temp_max,
                "temperatura_min": temp_min,
                "precipitacao_mm": precip_mm,
                "vento_kmh": wind_kmh,
                "condicao": condicao,
                "uv_index": uv,
                "score": score,
                "raw": {
                    "geocode": f"{lat},{lon}",
                    "api_endpoint": "v3/wx/forecast/daily/5day",
                    "data_type": "gridded_forecast",
                },
            })
        return results
    except Exception as e:
        logger.warning("Weather Underground forecast error: %s", e)
        return []


def fetch_all_forecasts_for_store(store: dict) -> list[dict]:
    lat = store.get("latitude")
    lon = store.get("longitude")
    name = store.get("name", "")
    address = store.get("address", "")
    store_id = store.get("id")
    all_results = []

    if lat is not None and lon is not None:
        om_fc = fetch_openmeteo_forecast(lat, lon)
        for rec in om_fc:
            rec["store_id"] = store_id
        all_results.extend(om_fc)

        aw_fc = fetch_accuweather_forecast(lat, lon)
        for rec in aw_fc:
            rec["store_id"] = store_id
        all_results.extend(aw_fc)

        wu_fc = fetch_wu_forecast(lat, lon)
        for rec in wu_fc:
            rec["store_id"] = store_id
        all_results.extend(wu_fc)

    ipma_fc = fetch_ipma_forecast(name, address, lat=lat, lon=lon)
    for rec in ipma_fc:
        rec["store_id"] = store_id
    all_results.extend(ipma_fc)

    return all_results


def compute_composite_score(records_for_day: list[dict]) -> dict:
    scores = [r["score"] for r in records_for_day if r.get("score") is not None]
    if not scores:
        return {"composite_score": None, "divergencia": None, "num_fontes": 0}
    composite = round(sum(scores) / len(scores), 1)
    divergencia = round(max(scores) - min(scores), 1) if len(scores) > 1 else 0.0
    return {
        "composite_score": composite,
        "divergencia": divergencia,
        "num_fontes": len(scores),
    }
