from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import create_app
from models import Car, Order, Service, User, db


def utc_days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def seed_database() -> None:
    app = create_app()

    with app.app_context():
        db.drop_all()
        db.create_all()

        password_method = "pbkdf2:sha256"

        admin = User(
            username="admin",
            password_hash=generate_password_hash("admin123", method=password_method),
            role="admin",
        )
        worker = User(
            username="worker_alex",
            password_hash=generate_password_hash("worker123", method=password_method),
            role="worker",
        )
        db.session.add_all([admin, worker])
        db.session.flush()

        cars = [
            Car(
                brand="Toyota",
                model="Camry 40",
                license_plate="01ABC123",
                owner_phone="+77011112233",
            ),
            Car(
                brand="BMW",
                model="X5",
                license_plate="02BMW777",
                owner_phone="+77023334455",
            ),
            Car(
                brand="Porsche",
                model="Cayenne",
                license_plate="03POR911",
                owner_phone="+77055556677",
            ),
            Car(
                brand="Mercedes-Benz",
                model="E-Class W213",
                license_plate="04MBZ888",
                owner_phone="+77078889900",
            ),
            Car(
                brand="Toyota",
                model="Camry 40",
                license_plate="040AAA01",
                owner_phone="+77015556677",
            ),
        ]
        db.session.add_all(cars)

        services = [
            Service(name="Үш қабатты полировка", price=60000.0, duration_mins=180),
            Service(name="Полировка кузова", price=45000.0, duration_mins=240),
            Service(name="Химчистка салона", price=28000.0, duration_mins=180),
            Service(name="Керамическое покрытие", price=95000.0, duration_mins=360),
            Service(name="Мойка + воск", price=12000.0, duration_mins=90),
        ]
        db.session.add_all(services)
        db.session.flush()

        orders = [
            Order(
                car_id=cars[0].id,
                user_id=worker.id,
                service_id=services[0].id,
                status="Pending",
                total_price=services[0].price,
                created_at=utc_days_ago(1),
            ),
            Order(
                car_id=cars[1].id,
                user_id=worker.id,
                service_id=services[1].id,
                status="In Progress",
                total_price=services[1].price,
                created_at=utc_days_ago(2),
            ),
            Order(
                car_id=cars[2].id,
                user_id=admin.id,
                service_id=services[2].id,
                status="Ready",
                total_price=services[2].price,
                created_at=utc_days_ago(3),
            ),
            Order(
                car_id=cars[3].id,
                user_id=worker.id,
                service_id=services[3].id,
                status="Ready",
                total_price=services[3].price,
                created_at=utc_days_ago(0),
            ),
            Order(
                car_id=cars[4].id,
                user_id=worker.id,
                service_id=services[0].id,
                status="In Progress",
                total_price=60000.0,
                created_at=utc_days_ago(0),
            ),
        ]
        db.session.add_all(orders)
        db.session.commit()

        print("База данных успешно заполнена тестовыми данными.")
        print(f"  Пользователи: {User.query.count()}")
        print(f"  Автомобили:   {Car.query.count()}")
        print(f"  Услуги:       {Service.query.count()}")
        print(f"  Заказы:       {Order.query.count()}")
        print()
        print("Учётные записи для входа:")
        print("  admin / admin123 (роль: admin)")
        print("  worker_alex / worker123 (роль: worker)")


if __name__ == "__main__":
    seed_database()
