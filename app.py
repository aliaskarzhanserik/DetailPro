import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import requests
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from markupsafe import Markup, escape
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from models import ORDER_STATUSES, Car, Order, Service, User, db

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "detailing_crm.db"

PHONE_PATTERN = re.compile(r"^\+7\d{10}$")
FOREX_API_URL = "https://open.er-api.com/v6/latest/USD"
DEFAULT_USD_KZT_RATE = 450.0
FOREX_CACHE_TTL_SECONDS = 3600

_forex_cache = {
    "rate": None,
    "source": None,
    "updated_at": None,
    "fetched_at": None,
}

STATUS_LABELS_RU = {
    "Pending": "В ожидании",
    "In Progress": "В работе",
    "Ready": "Готово",
}

STATUS_LABELS_KZ = {
    "Pending": "Күтуде",
    "In Progress": "Жұмыс үстінде",
    "Ready": "Дайын",
}

STATUS_KEYS = {
    "Pending": "pending",
    "In Progress": "in_progress",
    "Ready": "completed",
}

STATUS_KEY_TO_DB = {value: key for key, value in STATUS_KEYS.items()}
STATUS_NORMALIZED_TO_DB = {
    status.lower().replace(" ", "_"): status
    for status in STATUS_KEYS
}

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Для доступа необходимо войти в систему."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


class ApiError(Exception):
    def __init__(self, message: str, status_code: int = 400, errors: Optional[List] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.errors = errors or []


def user_is_admin() -> bool:
    return current_user.is_authenticated and current_user.is_admin()


def user_is_master() -> bool:
    return current_user.is_authenticated and current_user.is_master()


def get_scoped_user_id() -> Optional[int]:
    if not current_user.is_authenticated:
        return None
    if current_user.is_admin():
        return None
    return current_user.id


def ensure_order_access(order: Order) -> None:
    if user_is_admin():
        return
    if not current_user.is_authenticated or order.user_id != current_user.id:
        raise ApiError("Доступ запрещён.", status_code=403)


def admin_required_api() -> None:
    if not user_is_admin():
        raise ApiError("Доступ запрещён. Требуется роль admin.", status_code=403)


def is_api_request() -> bool:
    return request.path.startswith("/api/")


def error_response(message: str, status_code: int, errors: Optional[List] = None):
    payload = {
        "success": False,
        "error": {"message": message, "status_code": status_code},
    }
    if errors:
        payload["error"]["details"] = errors
    return jsonify(payload), status_code


def fetch_usd_kzt_rate_from_api() -> dict:
    try:
        response = requests.get(FOREX_API_URL, timeout=8)
        response.raise_for_status()
        payload = response.json()
        if payload.get("result") != "success":
            raise ValueError("API error")
        kzt_rate = payload.get("rates", {}).get("KZT")
        if kzt_rate is None or float(kzt_rate) <= 0:
            raise ValueError("KZT missing")
        return {
            "rate": float(kzt_rate),
            "source": "open.er-api.com",
            "updated_at": payload.get("time_last_update_utc"),
            "is_fallback": False,
        }
    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        return {
            "rate": DEFAULT_USD_KZT_RATE,
            "source": "fallback",
            "updated_at": None,
            "is_fallback": True,
            "error": str(error),
        }


def get_usd_kzt_exchange_info(force_refresh: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    cached_rate = _forex_cache.get("rate")
    fetched_at = _forex_cache.get("fetched_at")
    if (
        not force_refresh
        and cached_rate is not None
        and fetched_at is not None
        and (now - fetched_at).total_seconds() < FOREX_CACHE_TTL_SECONDS
    ):
        return {
            "rate": float(cached_rate),
            "source": _forex_cache.get("source") or "cache",
            "updated_at": _forex_cache.get("updated_at"),
            "is_fallback": _forex_cache.get("source") == "fallback",
        }
    forex_data = fetch_usd_kzt_rate_from_api()
    _forex_cache["rate"] = forex_data["rate"]
    _forex_cache["source"] = forex_data["source"]
    _forex_cache["updated_at"] = forex_data.get("updated_at")
    _forex_cache["fetched_at"] = now
    return forex_data


def convert_kzt_to_usd(amount_kzt: float, usd_kzt_rate: float) -> float:
    if usd_kzt_rate <= 0:
        return 0.0
    return round(float(amount_kzt) / float(usd_kzt_rate), 2)


def format_usd_price(amount_kzt: float, usd_kzt_rate: float) -> str:
    return f"${convert_kzt_to_usd(amount_kzt, usd_kzt_rate):,.2f}"


def send_client_notification(order_id: int, status: str) -> None:
    order = get_order_or_404(order_id)
    phone = order.car.owner_phone if order.car else "+7XXXXXXXXXX"
    brand = order.car.brand if order.car else "—"
    model = order.car.model if order.car else ""
    plate = order.car.license_plate if order.car else "—"
    status_ru = STATUS_LABELS_RU.get(status, status)
    message = (
        f"Ваша машина {brand} {model} [{plate}] переведена в статус [{status_ru}]!"
    )
    log_line = (
        f"\033[1m📱 [SMS-PUSH] Уведомление на номер {phone}: "
        f"Ваша машина {brand} {plate} переведена в статус [{status_ru}]!\033[0m"
    )
    print(log_line)
    print(f"    Текст: {message} (order_id={order_id})")


def get_json_body() -> dict:
    if not request.is_json:
        raise ApiError("Требуется JSON.")
    data = request.get_json(silent=True)
    if data is None or not isinstance(data, dict):
        raise ApiError("Некорректный JSON.")
    return data


def require_fields(data: dict, fields: List[str]) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        raise ApiError(
            "Отсутствуют поля.",
            errors=[{"field": f, "message": "Обязательно"} for f in missing],
        )


def validate_non_empty_string(value, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiError("Ошибка валидации.", errors=[{"field": field_name, "message": "Пустое"}])
    return value.strip()


def validate_owner_phone(phone: str) -> str:
    cleaned = validate_non_empty_string(phone, "owner_phone")
    normalized = re.sub(r"[^\d+]", "", cleaned)
    if normalized.startswith("8") and len(normalized) == 11:
        normalized = "+7" + normalized[1:]
    elif normalized.startswith("7") and len(normalized) == 11:
        normalized = "+" + normalized
    elif len(normalized) == 10 and normalized.isdigit():
        normalized = "+7" + normalized
    if not PHONE_PATTERN.match(normalized):
        raise ApiError(
            "Ошибка валидации.",
            errors=[{"field": "owner_phone", "message": "Формат: +7XXXXXXXXXX"}],
        )
    return normalized


def validate_license_plate(license_plate: str, car_id: Optional[int] = None) -> str:
    cleaned = validate_non_empty_string(license_plate, "license_plate").upper()
    existing = Car.query.filter_by(license_plate=cleaned).first()
    if existing and (car_id is None or existing.id != car_id):
        raise ApiError(
            "Ошибка валидации.",
            errors=[{"field": "license_plate", "message": "Госномер уже существует"}],
        )
    return cleaned


def get_db_order_status(status: str) -> str:
    cleaned = validate_non_empty_string(status, "status")
    if cleaned in STATUS_KEYS:
        return cleaned
    if cleaned in STATUS_KEY_TO_DB:
        return STATUS_KEY_TO_DB[cleaned]
    normalized = cleaned.lower().replace(" ", "_")
    if normalized in STATUS_KEY_TO_DB:
        return STATUS_KEY_TO_DB[normalized]
    if normalized in STATUS_NORMALIZED_TO_DB:
        return STATUS_NORMALIZED_TO_DB[normalized]
    raise ApiError("Неверный статус.", status_code=400)


def normalize_order_status(status: str) -> str:
    return get_db_order_status(status)


def validate_order_status(status: str) -> str:
    return get_db_order_status(status)


def validate_positive_int(value, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError("Ошибка валидации.", errors=[{"field": field_name, "message": "Число"}])
    if number <= 0:
        raise ApiError("Ошибка валидации.", errors=[{"field": field_name, "message": "> 0"}])
    return number


def normalize_license_plate(license_plate: str) -> str:
    return re.sub(r"[\s\-]", "", (license_plate or "")).upper()


def save_uploaded_image(order: Order, uploaded_file, field_name="image_url") -> str:
    if uploaded_file is None or uploaded_file.filename == "":
        raise ApiError("Файл не выбран.", status_code=400)

    filename = secure_filename(uploaded_file.filename)
    if not filename or "." not in filename:
        raise ApiError("Неверный формат файла.", status_code=400)

    file_ext = filename.rsplit(".", 1)[1].lower()
    allowed_extensions = {"jpg", "jpeg", "png", "gif", "webp"}
    if file_ext not in allowed_extensions:
        raise ApiError("Неверный формат файла. Допускаются: JPG, PNG, GIF, WEBP.", status_code=400)

    uploads_dir = BASE_DIR / "static" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    if order.id is None:
        db.session.flush()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    random_suffix = os.urandom(4).hex()
    filename = f"order_{order.id}_{field_name}_{timestamp}_{random_suffix}.{file_ext}"
    file_path = uploads_dir / filename
    uploaded_file.save(str(file_path))

    relative_path = f"uploads/{filename}"
    if field_name == "image_url":
        order.image_url = relative_path
    elif field_name == "photo_before":
        order.photo_before = relative_path
    elif field_name == "photo_after":
        order.photo_after = relative_path
    else:
        order.image_url = relative_path

    db.session.add(order)
    db.session.commit()
    return relative_path


def normalize_image_url(image_url: Optional[str]) -> Optional[str]:
    if not image_url:
        return None
    normalized = image_url.strip()
    if normalized.startswith("/static/"):
        return normalized
    if normalized.startswith("static/"):
        return "/" + normalized
    return f"/static/{normalized.lstrip('/')}"


def get_car_or_404(car_id: int) -> Car:
    car = db.session.get(Car, car_id)
    if car is None:
        raise ApiError(f"Автомобиль {car_id} не найден.", status_code=404)
    return car


def ensure_user_exists(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if user is None:
        raise ApiError("Сотрудник не найден.", status_code=404)
    return user


def get_default_order_worker_id() -> int:
    if not user_is_admin():
        return current_user.id

    worker = (
        User.query
        .filter(User.role.in_(["worker", "master"]))
        .order_by(User.username.asc())
        .first()
    )
    if worker:
        return worker.id
    return current_user.id


def resolve_quick_order_user_id(data: dict) -> int:
    raw_user_id = data.get("user_id")
    if raw_user_id in (None, ""):
        return get_default_order_worker_id()
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return get_default_order_worker_id()
    if not user_is_admin():
        return current_user.id
    user = db.session.get(User, user_id)
    if user is None:
        return get_default_order_worker_id()
    return user_id


def get_order_deadline_iso(order: Order) -> str:
    if order.created_at and order.service:
        deadline = order.created_at + timedelta(minutes=int(order.service.duration_mins))
        return deadline.isoformat()
    return ""


def get_order_or_404(order_id: int) -> Order:
    order = (
        Order.query.options(
            joinedload(Order.car),
            joinedload(Order.worker),
            joinedload(Order.service),
        )
        .filter_by(id=order_id)
        .first()
    )
    if order is None:
        raise ApiError(f"Заказ {order_id} не найден.", status_code=404)
    return order


def get_all_orders_with_relations(user_id: Optional[int] = None) -> List[Order]:
    query = Order.query.options(
        joinedload(Order.car),
        joinedload(Order.worker),
        joinedload(Order.service),
    )
    if user_id is not None:
        query = query.filter(Order.user_id == user_id)
    return query.order_by(Order.created_at.desc()).all()


def get_orders_count_by_status(status: str, user_id: Optional[int] = None) -> int:
    db_status = get_db_order_status(status)
    query = db.session.query(func.count(Order.id)).filter(Order.status == db_status)
    if user_id is not None:
        query = query.filter(Order.user_id == user_id)
    return int(query.scalar() or 0)


def get_total_cash_ready(user_id: Optional[int] = None) -> float:
    ready_status = get_db_order_status("completed")
    query = db.session.query(func.coalesce(func.sum(Order.total_price), 0.0)).filter(
        Order.status == ready_status
    )
    if user_id is not None:
        query = query.filter(Order.user_id == user_id)
    return float(query.scalar() or 0.0)


def get_master_leaderboard(user_id: Optional[int] = None) -> list:
    ready_status = get_db_order_status("completed")
    query = (
        db.session.query(
            User.id.label("user_id"),
            User.username.label("username"),
            func.count(Order.id).label("completed_count"),
        )
        .outerjoin(Order, (Order.user_id == User.id) & (Order.status == ready_status))
        .group_by(User.id, User.username)
    )
    if user_id is not None:
        query = query.filter(User.id == user_id)
    rows = query.order_by(func.count(Order.id).desc()).all()
    return [
        {
            "user_id": row.user_id,
            "username": row.username,
            "completed_count": int(row.completed_count or 0),
        }
        for row in rows
    ]


def get_revenue_chart_data(user_id: Optional[int] = None) -> dict:
    ready_status = get_db_order_status("completed")
    query = (
        db.session.query(
            func.date(Order.created_at).label("day"),
            func.coalesce(func.sum(Order.total_price), 0.0).label("revenue"),
        )
        .filter(Order.status == ready_status)
        .group_by(func.date(Order.created_at))
        .order_by(func.date(Order.created_at))
    )
    if user_id is not None:
        query = query.filter(Order.user_id == user_id)
    rows = query.all()
    labels = []
    values = []
    for row in rows:
        day_label = str(row.day) if row.day else "—"
        labels.append(day_label)
        values.append(float(row.revenue or 0))
    if not labels:
        labels = ["—"]
        values = [0.0]
    return {"labels": labels, "values": values}


def get_brand_pie_chart_data(user_id: Optional[int] = None) -> dict:
    query = (
        db.session.query(
            Car.brand.label("brand"),
            func.count(Order.id).label("order_count"),
        )
        .join(Car, Order.car_id == Car.id)
        .group_by(Car.brand)
        .order_by(func.count(Order.id).desc())
    )
    if user_id is not None:
        query = query.filter(Order.user_id == user_id)
    rows = query.limit(8).all()
    labels = [row.brand for row in rows] or ["Нет данных"]
    values = [int(row.order_count or 0) for row in rows] or [1]
    return {"labels": labels, "values": values}


def order_to_dict(order: Order, usd_kzt_rate: Optional[float] = None) -> dict:
    rate = usd_kzt_rate or get_usd_kzt_exchange_info()["rate"]
    total_price = float(order.total_price)
    deadline = None
    if order.created_at and order.service:
        deadline_dt = order.created_at + timedelta(minutes=int(order.service.duration_mins))
        deadline = deadline_dt.isoformat()
    return {
        "id": order.id,
        "car_id": order.car_id,
        "user_id": order.user_id,
        "service_id": order.service_id,
        "status": order.status,
        "status_key": STATUS_KEYS.get(order.status, order.status.lower().replace(" ", "_")),
        "total_price": total_price,
        "total_price_usd": convert_kzt_to_usd(total_price, rate),
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "deadline_at": deadline,
        "duration_mins": order.service.duration_mins if order.service else 0,
        "car_brand": order.car.brand if order.car else "",
        "car_model": order.car.model if order.car else "",
        "license_plate": order.car.license_plate if order.car else "",
        "owner_phone": order.car.owner_phone if order.car else "",
        "worker_name": order.worker.username if order.worker else "",
        "master_name": order.master.username if order.master else (order.worker.username if order.worker else ""),
        "service_name": order.service.name if order.service else "",
        "image_url": normalize_image_url(order.image_url) if order.image_url else "/static/images/default_car.jpg",
    }


def get_track_progress(status: str) -> dict:
    mapping = {
        "Pending": {
            "label": "В ожидании",
            "label_key": "statusPending",
            "percent": 25,
            "current_step": 1,
            "color": "#f59e0b",
            "status_class": "pending",
        },
        "In Progress": {
            "label": "В работе",
            "label_key": "statusProgress",
            "percent": 60,
            "current_step": 2,
            "color": "#0d6efd",
            "status_class": "in-progress",
        },
        "Ready": {
            "label": "Готово",
            "label_key": "statusReady",
            "percent": 100,
            "current_step": 3,
            "color": "#198754",
            "status_class": "completed",
        },
    }
    return mapping.get(status, mapping["Pending"])


def build_track_context(license_plate: str = "") -> dict:
    forex = get_usd_kzt_exchange_info()
    car = None
    order = None
    error_message = None
    receipt = None
    progress = get_track_progress("Pending")
    status_steps = [
        {"step": 1, "label_key": "trackStepReceived", "label": "Принято"},
        {"step": 2, "label_key": "trackStepInProgress", "label": "В работе"},
        {"step": 3, "label_key": "trackStepReady", "label": "Готово"},
    ]

    if license_plate:
        normalized = normalize_license_plate(license_plate)
        for item in Car.query.all():
            if normalize_license_plate(item.license_plate) == normalized:
                car = item
                break
        if car is None:
            error_message = "Автомобиль не найден."
        else:
            order = (
                Order.query.options(
                    joinedload(Order.car),
                    joinedload(Order.worker),
                    joinedload(Order.service),
                )
                .filter_by(car_id=car.id)
                .order_by(Order.created_at.desc())
                .first()
            )
            if order is None:
                error_message = "Активный заказ не найден."
            else:
                order.image_url = normalize_image_url(order.image_url)
                receipt = build_receipt_data(order, forex["rate"])
                progress = get_track_progress(order.status)

    return {
        "search_plate": license_plate,
        "car": car,
        "order": order,
        "error_message": error_message,
        "receipt": receipt,
        "usd_kzt_rate": forex["rate"],
        "status_labels": STATUS_LABELS_RU,
        "progress": progress,
        "status_steps": status_steps,
    }


def build_receipt_data(order: Order, usd_kzt_rate: float) -> dict:
    return {
        "order_id": order.id,
        "license_plate": order.car.license_plate,
        "brand": order.car.brand,
        "model": order.car.model,
        "service_name": order.service.name,
        "worker_name": order.worker.username,
        "status": STATUS_LABELS_RU.get(order.status, order.status),
        "total_price": float(order.total_price),
        "total_price_usd": convert_kzt_to_usd(float(order.total_price), usd_kzt_rate),
        "created_at": order.created_at.strftime("%d.%m.%Y %H:%M") if order.created_at else "—",
        "owner_phone": order.car.owner_phone,
    }


def build_dashboard_context() -> dict:
    scoped_user_id = get_scoped_user_id()
    is_admin = user_is_admin()
    forex = get_usd_kzt_exchange_info()
    usd_kzt_rate = forex["rate"]
    total_cash = get_total_cash_ready(scoped_user_id if not is_admin else None)
    if is_admin:
        total_cash = get_total_cash_ready()

    orders = get_all_orders_with_relations(scoped_user_id)
    pending_status = get_db_order_status("pending")
    in_progress_status = get_db_order_status("in_progress")
    ready_status = get_db_order_status("completed")
    orders_pending = [o for o in orders if o.status == pending_status]
    orders_in_progress = [o for o in orders if o.status == in_progress_status]
    orders_ready = [o for o in orders if o.status == ready_status]

    workers = (
        User.query
        .filter(User.role.in_(["worker", "master"]))
        .order_by(User.username.asc())
        .all()
    )
    masters = (
        User.query
        .filter(User.role == "master")
        .order_by(User.username.asc())
        .all()
    )
    services = Service.query.order_by(Service.name.asc()).all()

    return {
        "active_page": "dashboard",
        "is_admin": is_admin,
        "current_user": current_user,
        "usd_kzt_rate": usd_kzt_rate,
        "total_cash": total_cash,
        "total_cash_usd": convert_kzt_to_usd(total_cash, usd_kzt_rate),
        "pending_queue": get_orders_count_by_status("Pending", scoped_user_id),
        "cars_in_progress": get_orders_count_by_status("In Progress", scoped_user_id),
        "completed_orders": get_orders_count_by_status("Ready", scoped_user_id),
        "my_total_tasks": (
            get_orders_count_by_status("Pending", scoped_user_id)
            + get_orders_count_by_status("In Progress", scoped_user_id)
            + get_orders_count_by_status("Ready", scoped_user_id)
        ),
        "master_leaderboard": get_master_leaderboard(None if is_admin else scoped_user_id),
        "revenue_chart": get_revenue_chart_data(None if is_admin else scoped_user_id),
        "brand_chart": get_brand_pie_chart_data(None if is_admin else scoped_user_id),
        "orders_pending": orders_pending,
        "orders_in_progress": orders_in_progress,
        "orders_ready": orders_ready,
        "workers": workers,
        "masters": masters,
        "services": services,
        "orders_json": [order_to_dict(o, usd_kzt_rate) for o in orders],
    }


def build_master_dashboard_context() -> dict:
    if not user_is_master():
        raise ApiError("Доступ запрещён.", status_code=403)

    forex = get_usd_kzt_exchange_info()
    usd_kzt_rate = forex["rate"]
    orders = get_all_orders_with_relations(current_user.id)
    pending_status = get_db_order_status("pending")
    in_progress_status = get_db_order_status("in_progress")
    ready_status = get_db_order_status("completed")
    orders_pending = [o for o in orders if o.status == pending_status]
    orders_in_progress = [o for o in orders if o.status == in_progress_status]
    orders_ready = [o for o in orders if o.status == ready_status]
    total_cash = get_total_cash_ready(current_user.id)

    return {
        "active_page": "master_dashboard",
        "is_admin": False,
        "is_master": True,
        "current_user": current_user,
        "usd_kzt_rate": usd_kzt_rate,
        "total_cash": total_cash,
        "total_cash_usd": convert_kzt_to_usd(total_cash, usd_kzt_rate),
        "pending_queue": len(orders_pending),
        "cars_in_progress": len(orders_in_progress),
        "completed_orders": len(orders_ready),
        "orders": orders,
        "orders_pending": orders_pending,
        "orders_in_progress": orders_in_progress,
        "orders_ready": orders_ready,
        "status_labels": STATUS_LABELS_KZ,
    }


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        if is_api_request():
            return error_response(error.message, error.status_code, error.errors)
        return render_template("error.html", status_code=error.status_code, message=error.message), error.status_code

    @app.errorhandler(404)
    def handle_404(error):
        if is_api_request():
            return error_response("Не найдено.", 404)
        return render_template("error.html", status_code=404, message="Страница не найдена."), 404

    @app.errorhandler(500)
    def handle_500(error):
        app.logger.error(traceback.format_exc())
        if is_api_request():
            return error_response("Ошибка сервера.", 500)
        return render_template("error.html", status_code=500, message="Ошибка сервера."), 500


def register_routes(app: Flask) -> None:
    @app.route("/track", methods=["GET", "POST"])
    def track():
        license_plate = request.form.get("license_plate", "").strip() if request.method == "POST" else request.args.get("plate", "").strip()
        context = build_track_context(license_plate)
        return render_template("track.html", **context)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and check_password_hash(user.password_hash, password):
                login_user(user)
                nxt = request.args.get("next")
                return redirect(nxt if nxt and nxt.startswith("/") else url_for("dashboard"))
            flash("Неверный логин или пароль.", "danger")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return render_template("dashboard.html", **build_dashboard_context())

    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template("dashboard.html", **build_dashboard_context())

    @app.route("/master/dashboard")
    @login_required
    def master_dashboard():
        if not user_is_master():
            raise ApiError("Доступ запрещён.", status_code=403)
        return render_template("master_dashboard.html", **build_master_dashboard_context())

    @app.route("/add_master", methods=["POST"])
    @login_required
    def add_master():
        if not user_is_admin():
            raise ApiError("Доступ запрещён.", status_code=403)

        if request.is_json:
            data = get_json_body()
        else:
            data = request.form.to_dict(flat=True)

        require_fields(data, ["username", "phone", "password"])
        username = validate_non_empty_string(data["username"], "username")
        phone = validate_owner_phone(data["phone"])
        password = validate_non_empty_string(data["password"], "password")
        role = data.get("role", "master")
        if role not in ("admin", "worker", "master"):
            raise ApiError("Неверная роль пользователя.", status_code=400)

        existing_user = User.query.filter_by(username=username).first()
        if existing_user is not None:
            raise ApiError("Пользователь с таким именем уже существует.", status_code=400)

        new_user = User(
            username=username,
            password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
            role=role,
        )
        if hasattr(new_user, "phone"):
            new_user.phone = phone

        db.session.add(new_user)
        db.session.commit()

        if request.is_json:
            return jsonify({"success": True, "data": {"id": new_user.id, "username": new_user.username, "role": new_user.role}}), 201

        flash("Мастер успешно добавлен.", "success")
        return redirect(url_for("dashboard"))

    @app.route('/api/v1/masters/<int:id>/delete', methods=['POST'])
    @login_required
    def delete_master(id: int):
        if not user_is_admin():
            raise ApiError("Доступ запрещён.", status_code=403)

        try:
            master = User.query.get_or_404(id)
            if master.role != 'master':
                raise ApiError("Только мастера можно удалять через этот маршрут.", status_code=400)

            db.session.delete(master)
            db.session.commit()
            return jsonify({'status': 'success'}), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({'status': 'error', 'message': str(e)}), 500

    @app.route("/api/v1/dashboard-data", methods=["GET"])
    @login_required
    def dashboard_data_api():
        ctx = build_dashboard_context()
        return jsonify({"success": True, "data": ctx})

    @app.route("/api/v1/add-car", methods=["POST"])
    @app.route("/api/v1/orders/create", methods=["POST"])
    @login_required
    def create_order():
        data = request.form.to_dict(flat=True)
        require_fields(data, ["license_plate", "car_model", "service_name", "price"])
        license_plate = normalize_license_plate(data["license_plate"])
        if not license_plate:
            raise ApiError(
                "Ошибка валидации.",
                errors=[{"field": "license_plate", "message": "Госномер не может быть пустым"}],
            )
        car_model = validate_non_empty_string(data["car_model"], "car_model")
        service_name = validate_non_empty_string(data["service_name"], "service_name")
        try:
            price = float(data["price"])
        except (TypeError, ValueError):
            raise ApiError(
                "Ошибка валидации.",
                errors=[{"field": "price", "message": "Число"}],
            )
        if price < 0:
            raise ApiError(
                "Ошибка валидации.",
                errors=[{"field": "price", "message": "Не может быть отрицательным"}],
            )
        service = (
            Service.query
            .filter(func.lower(Service.name) == service_name.lower())
            .first()
        )
        if service is None:
            service = Service(name=service_name, price=price, duration_mins=60)
            db.session.add(service)
            db.session.flush()
        else:
            service.price = price

        car = Car.query.filter_by(license_plate=license_plate).first()
        if car is None:
            car = Car(brand=car_model, model="", license_plate=license_plate, owner_phone="")
            db.session.add(car)
            db.session.flush()
        else:
            car.brand = car_model
            if not car.model:
                car.model = ""

        if current_user.is_admin():
            user_id = get_default_order_worker_id()
        else:
            user_id = current_user.id

        order = Order(
            car_id=car.id,
            user_id=user_id,
            service_id=service.id,
            status="Pending",
            total_price=float(price),
        )
        db.session.add(order)
        db.session.commit()
        if request.files.get("photo"):
            try:
                save_uploaded_image(order, request.files.get("photo"), field_name="image_url")
            except ApiError:
                pass
        return jsonify({"success": True, "data": order_to_dict(order)})

    @app.route('/api/v1/masters/create', methods=['POST'])
    @login_required
    def create_master():
        if not user_is_admin():
            raise ApiError('Доступ запрещён.', status_code=403)
        if request.is_json:
            data = get_json_body()
        else:
            data = request.form.to_dict(flat=True)
        require_fields(data, ['username', 'password'])
        username = validate_non_empty_string(data['username'], 'username')
        password = validate_non_empty_string(data['password'], 'password')
        existing_user = User.query.filter_by(username=username).first()
        if existing_user is not None:
            raise ApiError('Пользователь с таким именем уже существует.', status_code=400)
        new_user = User(
            username=username,
            password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
            role='master',
        )
        db.session.add(new_user)
        db.session.commit()
        return jsonify({'success': True, 'data': {'id': new_user.id, 'username': new_user.username}}), 201

    @app.route('/api/v1/orders/<int:order_id>/start', methods=['POST'])
    @login_required
    def start_order(order_id: int):
        if current_user.role not in ['admin', 'master']:
            raise ApiError('Доступ запрещён.', status_code=403)
        order = get_order_or_404(order_id)
        if order.status != 'Pending':
            raise ApiError('Заказ не находится в ожидании.', status_code=400)
        if request.is_json:
            body = get_json_body()
        else:
            body = request.form.to_dict(flat=True)
        require_fields(body, ['master_id'])
        master_id = validate_positive_int(body['master_id'], 'master_id')
        master = db.session.get(User, master_id)
        if master is None or master.role != 'master':
            raise ApiError('Мастер не найден.', status_code=404)
        order.user_id = master.id
        order.status = 'In Progress'
        db.session.commit()
        return jsonify({'success': True, 'data': order_to_dict(order)}), 200

    @app.route('/api/v1/orders/<int:order_id>/complete', methods=['POST'])
    @login_required
    def complete_order(order_id: int):
        if current_user.role not in ['admin', 'master']:
            raise ApiError('Доступ запрещён.', status_code=403)
        order = get_order_or_404(order_id)
        order.status = validate_order_status('completed')
        db.session.commit()
        rate = get_usd_kzt_exchange_info()["rate"]
        return jsonify({
            'status': 'success',
            'brand': order.car.brand if order.car else '',
            'plate': order.car.license_plate if order.car else '',
            'service': order.service.name if order.service else '',
            'price': "{:,.0f}".format(order.total_price).replace(",", " "),
            'master': order.worker.username if order.worker else 'Белгісіз'
        }), 200

    @app.route("/api/v1/orders/<int:order_id>/status", methods=["PATCH"])
    @login_required
    def patch_order_status(order_id: int):
        order = get_order_or_404(order_id)
        ensure_order_access(order)
        data = get_json_body()
        require_fields(data, ["status"])
        new_status = validate_order_status(data["status"])
        old_status = order.status
        order.status = new_status
        db.session.commit()
        if old_status != new_status:
            send_client_notification(order.id, new_status)
        order = get_order_or_404(order.id)
        return jsonify({"success": True, "data": order_to_dict(order), "message": "Статус заказа успешно обновлен!"})

    @app.route("/api/v1/quick-order", methods=["POST"])
    @login_required
    def quick_order():
        if request.is_json:
            data = get_json_body()
        else:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                data = request.form.to_dict(flat=True)
        require_fields(data, ["brand", "model", "license_plate", "owner_phone", "service_id"])
        brand = validate_non_empty_string(data["brand"], "brand")
        model = validate_non_empty_string(data["model"], "model")
        plate = normalize_license_plate(data["license_plate"])
        if not plate:
            raise ApiError(
                "Ошибка валидации.",
                errors=[{"field": "license_plate", "message": "Госномер не может быть пустым"}],
            )
        phone = validate_owner_phone(data["owner_phone"])
        service_id = validate_positive_int(data["service_id"], "service_id")
        user_id = resolve_quick_order_user_id(data)
        if not user_is_admin() and user_id != current_user.id:
            user_id = current_user.id
        service = db.session.get(Service, service_id)
        if service is None:
            raise ApiError("Услуга не найдена.", status_code=404)
        ensure_user_exists(user_id)

        car = next(
            (c for c in Car.query.all() if normalize_license_plate(c.license_plate) == plate),
            None,
        )
        if car is None:
            car = Car(brand=brand, model=model, license_plate=plate, owner_phone=phone)
            db.session.add(car)
            db.session.flush()
        else:
            car.brand = brand
            car.model = model
            car.owner_phone = phone

        order = Order(
            car_id=car.id,
            user_id=user_id,
            service_id=service.id,
            status="Pending",
            total_price=float(service.price),
        )
        db.session.add(order)
        db.session.commit()
        if request.files.get("photo"):
            try:
                save_uploaded_image(order, request.files.get("photo"), field_name="image_url")
            except ApiError:
                pass
        order = get_order_or_404(order.id)
        send_client_notification(order.id, order.status)
        return (
            jsonify({"status": "success", "success": True, "data": order_to_dict(order)}),
            201,
        )

    @app.route("/api/v1/orders", methods=["GET"])
    @login_required
    def list_orders():
        orders = get_all_orders_with_relations(get_scoped_user_id())
        return jsonify({"success": True, "data": [order_to_dict(o) for o in orders]})

    @app.route("/api/v1/orders/<int:order_id>", methods=["GET"])
    @login_required
    def get_order(order_id: int):
        order = get_order_or_404(order_id)
        return jsonify({"success": True, "data": order_to_dict(order)})

    @app.route("/api/v1/orders/<int:order_id>/status", methods=["POST"])
    @login_required
    def post_order_status(order_id: int):
        order = get_order_or_404(order_id)
        ensure_order_access(order)
        data = get_json_body()
        require_fields(data, ["status"])
        new_status = validate_order_status(data["status"])
        old_status = order.status
        order.status = new_status
        db.session.commit()
        if old_status != new_status:
            send_client_notification(order.id, new_status)
        order = get_order_or_404(order.id)
        return jsonify({"success": True, "data": order_to_dict(order), "message": "Статус заказа успешно обновлен!"})

    @app.route("/api/v1/orders/<int:order_id>/archive", methods=["POST"])
    @login_required
    def post_archive_order(order_id: int):
        order = get_order_or_404(order_id)
        ensure_order_access(order)
        new_status = validate_order_status('completed')
        old_status = order.status
        order.status = new_status
        db.session.commit()
        if old_status != new_status:
            send_client_notification(order.id, new_status)
        order = get_order_or_404(order.id)
        return jsonify({"success": True, "data": order_to_dict(order), "message": "Заказ отправлен в архив."})

    @app.route("/api/v1/orders/<int:order_id>", methods=["PUT"])
    @login_required
    def update_order(order_id: int):
        order = get_order_or_404(order_id)
        ensure_order_access(order)
        if request.is_json:
            data = get_json_body()
        else:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                data = request.form.to_dict(flat=True)
        require_fields(data, ["car_id", "user_id", "service_id", "status", "total_price"])
        old_status = order.status
        order.car_id = validate_positive_int(data["car_id"], "car_id")
        order.user_id = validate_positive_int(data["user_id"], "user_id")
        if not user_is_admin():
            order.user_id = current_user.id
        order.service_id = validate_positive_int(data["service_id"], "service_id")
        order.status = validate_order_status(data["status"])
        order.total_price = float(data["total_price"])
        if order.total_price < 0:
            raise ApiError("Цена не может быть отрицательной.")
        if request.files.get("photo"):
            save_uploaded_image(order, request.files.get("photo"), field_name="image_url")
        db.session.commit()
        if old_status != order.status:
            send_client_notification(order.id, order.status)
        return jsonify({"success": True, "data": order_to_dict(get_order_or_404(order_id))})

    @app.route("/api/v1/orders/<int:order_id>", methods=["DELETE"])
    @login_required
    def delete_order(order_id: int):
        if current_user.role != 'admin':
            raise ApiError('Доступ запрещён. Только администратор может удалять заказы.', status_code=403)
        try:
            order = get_order_or_404(order_id)
            db.session.delete(order)
            db.session.commit()
            return jsonify({'success': True, 'message': 'Заказ удалён'}), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route("/api/v1/analytics", methods=["GET"])
    @login_required
    def analytics_api():
        uid = get_scoped_user_id()
        admin = user_is_admin()
        return jsonify({
            "success": True,
            "data": {
                "total_cash": get_total_cash_ready(None if admin else uid),
                "pending_queue": get_orders_count_by_status("Pending", uid),
                "cars_in_progress": get_orders_count_by_status("In Progress", uid),
                "completed_orders": get_orders_count_by_status("Ready", uid),
                "revenue_chart": get_revenue_chart_data(None if admin else uid),
                "brand_chart": get_brand_pie_chart_data(None if admin else uid),
                "master_leaderboard": get_master_leaderboard(None if admin else uid),
            },
        })

    @app.route("/upload_photo/<int:order_id>/<string:photo_type>", methods=["POST"])
    @login_required
    def upload_photo(order_id: int, photo_type: str):
        if current_user.role not in ['admin', 'master']:
            raise ApiError('Доступ запрещён.', status_code=403)
        order = get_order_or_404(order_id)
        ensure_order_access(order)
        
        if photo_type not in ("before", "after"):
            raise ApiError("Неверный тип фото. Допускается: 'before' или 'after'.", status_code=400)
        
        uploaded_file = request.files.get("photo") or request.files.get("file")
        if uploaded_file is None:
            raise ApiError("Файл не найден.", status_code=400)
        if uploaded_file.filename == "":
            raise ApiError("Файл не выбран.", status_code=400)
        
        allowed_extensions = {"jpg", "jpeg", "png", "gif", "webp"}
        if "." not in uploaded_file.filename or uploaded_file.filename.rsplit(".", 1)[1].lower() not in allowed_extensions:
            raise ApiError("Неверный формат файла. Допускаются: JPG, PNG, GIF, WEBP.", status_code=400)
        

        uploads_dir = Path(__file__).resolve().parent / "static" / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        random_suffix = os.urandom(4).hex()
        file_ext = uploaded_file.filename.rsplit(".", 1)[1].lower()
        filename = f"order_{order_id}_{photo_type}_{timestamp}_{random_suffix}.{file_ext}"
        
        file_path = uploads_dir / filename
        uploaded_file.save(str(file_path))
        
        relative_path = f"uploads/{filename}"
        if photo_type == "before":
            order.photo_before = relative_path
        else:
            order.photo_after = relative_path
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "data": {
                "order_id": order.id,
                "photo_type": photo_type,
                "filename": filename,
                "path": relative_path
            }
        }), 201


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", f"sqlite:///{DATABASE_PATH}")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    login_manager.init_app(app)

    @login_manager.unauthorized_handler
    def unauthorized():
        if is_api_request():
            return error_response("Требуется авторизация.", 401)
        return redirect(url_for("login", next=request.path))

    with app.app_context():
        db.create_all()

    @app.template_filter("order_deadline")
    def order_deadline_filter(order):
        return get_order_deadline_iso(order)

    @app.template_filter("format_datetime")
    def format_datetime_filter(value):
        if not value:
            return "—"
        try:
            return value.strftime("%d.%m.%Y %H:%M")
        except AttributeError:
            return str(value)

    @app.template_filter("format_usd_short")
    def format_usd_short_filter(amount_kzt):
        forex = get_usd_kzt_exchange_info()
        return f"${convert_kzt_to_usd(float(amount_kzt or 0), forex['rate']):,.2f}"

    @app.template_filter("format_duration")
    def format_duration_filter(minutes):
        try:
            mins = int(minutes)
        except (TypeError, ValueError):
            return "—"
        if mins < 60:
            return f"{mins} мин"
        hours = mins // 60
        rest = mins % 60
        if rest == 0:
            return f"{hours} ч"
        return f"{hours} ч {rest} мин"

    @app.template_filter("order_status_key")
    def order_status_key_filter(status):
        return STATUS_KEYS.get(status, str(status).strip().lower().replace(" ", "_"))

    @app.template_filter("format_kz_plate")
    def format_kz_plate_filter(license_plate):
        plate = normalize_license_plate(str(license_plate or ""))
        if not plate:
            return ""
        region = None
        main = plate
        if len(plate) >= 3 and plate[-2:].isdigit():
            region = plate[-2:]
            main = plate[:-2]
        if not main and region:
            main = region
            region = None
        safe_main = escape(main)
        safe_region = escape(region) if region else None
        flag_html = "<span class=\"flag\"></span><span class=\"kz-text\">KZ</span>"
        if safe_region:
            html = (
                f"<span class=\"kz-plate\">"
                f"<span class=\"flag-section\">{flag_html}</span>"
                f"<span class=\"number-main\">{safe_main}</span>"
                f"<span class=\"region-section\">{safe_region}</span>"
                f"</span>"
            )
        else:
            html = (
                f"<span class=\"kz-plate\">"
                f"<span class=\"flag-section\">{flag_html}</span>"
                f"<span class=\"number-main\">{safe_main}</span>"
                f"</span>"
            )
        return Markup(html)

    @app.context_processor
    def inject_globals():
        forex = get_usd_kzt_exchange_info()
        return {"usd_kzt_rate": forex["rate"]}

    register_error_handlers(app)
    register_routes(app)
    return app


app = create_app()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
