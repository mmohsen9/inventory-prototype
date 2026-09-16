"""
التطبيق الرئيسي - نظام إدخال حركات المخزون (نموذج أولي)
تشغيل: python app.py
"""
import os
import json
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect, url_for, session, jsonify, flash
)
from werkzeug.utils import secure_filename

from models import (
    db, User, Location, Item, PurchaseTransaction, PurchaseLine,
    TransferOrder, TransferLine, NewItemRequest, CountSession, CountLine,
    SyncQueue, SyncLog,
)
import validation_gate as vg
import sync_queue as sq

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'rafa_inventory.db')}"
)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB لكل رفع

db.init_app(app)


def ensure_seed_data():
    """يهيئ البيانات الأولية الأساسية إذا كانت قاعدة البيانات فارغة."""
    if Location.query.count() == 0:
        default_locations = [
            ("المستودع الرئيسي", "Warehouse 1", "warehouse"),
            ("رفاء", "RAFA", "branch"),
            ("التخصصي", "Takhassusi", "branch"),
            ("هيتن", "Hittin", "branch"),
            ("الصحافة", "Sahafa", "branch"),
            ("النزهة", "Nuzha", "branch"),
            ("الياسمين", "Yasmin", "branch"),
        ]
        for name_ar, name_en, location_type in default_locations:
            db.session.add(Location(name_ar=name_ar, name_en=name_en, type=location_type))
        db.session.commit()

    # تحميل الأصناف من ملف items_export.csv (بدون هذه الخطوة لا يمكن إضافة أي صنف للحركات)
    if Item.query.count() == 0:
        csv_path = os.path.join(BASE_DIR, "items_export.csv")
        if os.path.exists(csv_path):
            import csv as _csv

            def _to_float(value, default=0.0):
                try:
                    return float(str(value).strip())
                except (TypeError, ValueError):
                    return default

            with open(csv_path, encoding="utf-8-sig") as fh:
                loaded = 0
                for row in _csv.DictReader(fh):
                    sku = (row.get("sku") or "").strip()
                    if not sku:
                        continue
                    db.session.add(Item(
                        foodics_id=(row.get("id") or "").strip() or None,
                        sku=sku,
                        name_en=(row.get("name") or "").strip(),
                        name_ar=(row.get("name_localized") or "").strip() or (row.get("name") or "").strip(),
                        storage_unit=(row.get("storage_unit") or "").strip(),
                        ingredient_unit=(row.get("ingredient_unit") or "").strip(),
                        storage_to_ingredient_factor=_to_float(row.get("storage_to_ingredient_factor"), 1.0) or 1.0,
                        cost=_to_float(row.get("cost"), 0.0),
                        barcode=(row.get("barcode") or "").strip(),
                        category_reference=(row.get("category_reference") or "").strip(),
                    ))
                    loaded += 1
            db.session.commit()
            print(f"تم تحميل {loaded} صنفاً من items_export.csv.")
        else:
            print("تحذير: لم يُعثر على items_export.csv، لن تتوفر أصناف للاختيار.")

    warehouse_loc = Location.query.filter_by(type="warehouse").first()
    branch_loc = Location.query.filter_by(type="branch").first()
    default_users = [
        ("أمين المستودع", "warehouse", warehouse_loc.id if warehouse_loc else None),
        ("موظف الفرع", "branch_staff", branch_loc.id if branch_loc else None),
        ("المحاسب", "accountant", None),
    ]
    # الفحص بالاسم يمنع تكرار المستخدمين عند إقلاع أكثر من worker في نفس اللحظة
    for name, role, loc_id in default_users:
        if not User.query.filter_by(name=name).first():
            db.session.add(User(name=name, role=role, location_id=loc_id, password="1234", active=True))

    db.session.commit()


# ---------------------------------------------------------------
# أدوات مساعدة
# ---------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u or u.role not in roles:
                flash("لا تملك صلاحية الوصول لهذه الصفحة", "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def save_attachment(file_storage, subfolder):
    """
    يحفظ المرفق محلياً ويُرجع رابطاً/مساراً.
    ============================================================
    نقطة الاستبدال للربط الفعلي مع Google Drive API لاحقاً:
    ارفع الملف عبر Drive API هنا وأرجع رابط المشاركة بدلاً من المسار المحلي.
    ============================================================
    """
    if not file_storage or file_storage.filename == "":
        return None
    folder = os.path.join(UPLOAD_DIR, subfolder)
    os.makedirs(folder, exist_ok=True)
    filename = secure_filename(f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{file_storage.filename}")
    path = os.path.join(folder, filename)
    file_storage.save(path)
    return f"/uploads/{subfolder}/{filename}"


# ---------------------------------------------------------------
# تسجيل الدخول
# ---------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("dashboard") if current_user() else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name")
        password = request.form.get("password")
        user = User.query.filter_by(name=name, active=True).first()
        if user and user.password == password:
            session["user_id"] = user.id
            return redirect(url_for("dashboard"))
        flash("بيانات الدخول غير صحيحة", "error")
    users = User.query.filter_by(active=True).all()
    return render_template("login.html", users=users)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------
# لوحة التحكم الرئيسية
# ---------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    context = {"user": u}
    if u.role == "accountant":
        context["pending_purchases"] = PurchaseTransaction.query.filter_by(status="submitted").count()
        context["pending_transfers"] = TransferOrder.query.filter_by(status="needs_review").count()
        context["pending_items"] = NewItemRequest.query.filter_by(status="pending").count()
        context["failed_sync"] = SyncQueue.query.filter_by(status="failed").count()
        context["queued_sync"] = SyncQueue.query.filter(SyncQueue.status.in_(["queued", "retrying"])).count()
    return render_template("dashboard.html", **context)


# ---------------------------------------------------------------
# API مساعد: البحث عن صنف بالـ SKU/الباركود (يستخدمه JS أثناء المسح)
# ---------------------------------------------------------------
@app.route("/api/lookup_item")
@login_required
def api_lookup_item():
    code = request.args.get("code", "").strip()
    item = vg.check_item_exists(code)
    if not item:
        return jsonify({"found": False})
    return jsonify({
        "found": True,
        "id": item.id,
        "sku": item.sku,
        "name_ar": item.name_ar,
        "name_en": item.name_en,
        "unit": item.storage_unit,
        "cost": item.cost,
    })


# ---------------------------------------------------------------
# المشتريات
# ---------------------------------------------------------------
@app.route("/purchases")
@login_required
def purchases_list():
    u = current_user()
    q = PurchaseTransaction.query
    if u.role != "accountant":
        q = q.filter_by(created_by=u.id)
    purchases = q.order_by(PurchaseTransaction.created_at.desc()).all()
    return render_template("purchases_list.html", purchases=purchases)


@app.route("/purchases/new", methods=["GET", "POST"])
@login_required
def purchase_new():
    u = current_user()
    locations = Location.query.all()

    if request.method == "POST":
        location_id = int(request.form.get("location_id") or (u.location_id or 0))
        supplier = request.form.get("supplier_name")
        invoice_number = request.form.get("invoice_number")
        invoice_date = request.form.get("invoice_date")

        purchase = PurchaseTransaction(
            location_id=location_id,
            supplier_name=supplier,
            invoice_number=invoice_number,
            invoice_date=datetime.strptime(invoice_date, "%Y-%m-%d").date() if invoice_date else None,
            created_by=u.id,
            status="draft",
            attachments="[]",
        )
        db.session.add(purchase)
        db.session.commit()
        return redirect(url_for("purchase_edit", purchase_id=purchase.id))

    return render_template("purchase_new.html", locations=locations, user=u)


@app.route("/purchases/<int:purchase_id>", methods=["GET", "POST"])
@login_required
def purchase_edit(purchase_id):
    purchase = PurchaseTransaction.query.get_or_404(purchase_id)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_line":
            code = request.form.get("code", "").strip()
            qty = float(request.form.get("quantity") or 0)
            unit = request.form.get("unit")
            ok, item, msg = vg.validate_line(code, qty, purchase.location_id, unit)
            if not item:
                flash(msg, "error")
            else:
                line = PurchaseLine(
                    purchase_id=purchase.id, item_id=item.id,
                    quantity=qty, unit_cost=item.cost or 0,
                )
                db.session.add(line)
                db.session.commit()
                if msg:
                    flash(msg, "warning")

        elif action == "delete_line":
            line_id = int(request.form.get("line_id"))
            line = PurchaseLine.query.get(line_id)
            if line and line.purchase_id == purchase.id:
                db.session.delete(line)
                db.session.commit()

        elif action == "upload_attachment":
            file = request.files.get("attachment")
            path = save_attachment(file, f"purchases/{purchase.id}")
            if path:
                atts = json.loads(purchase.attachments or "[]")
                atts.append(path)
                purchase.attachments = json.dumps(atts)
                db.session.commit()
                flash("تم رفع المرفق بنجاح", "success")

        elif action == "submit_for_review":
            ok, errors = vg.validate_purchase_before_submit(purchase)
            if not ok:
                for e in errors:
                    flash(e, "error")
            else:
                purchase.status = "submitted"
                db.session.commit()
                flash("تم إرسال الفاتورة للمراجعة", "success")
                return redirect(url_for("purchases_list"))

        return redirect(url_for("purchase_edit", purchase_id=purchase.id))

    return render_template("purchase_edit.html", purchase=purchase,
                            attachments=json.loads(purchase.attachments or "[]"))


@app.route("/purchases/<int:purchase_id>/review", methods=["POST"])
@login_required
@role_required("accountant")
def purchase_review(purchase_id):
    purchase = PurchaseTransaction.query.get_or_404(purchase_id)
    action = request.form.get("action")

    if action == "approve":
        ok, errors = vg.validate_purchase_before_posting(purchase, purchase.location_id)
        if not ok:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("accountant_review"))
        purchase.status = "queued"
        db.session.commit()
        sq.enqueue("purchase", purchase.id)
        flash("تم اعتماد الفاتورة وإدراجها في طابور الترحيل", "success")

    elif action == "reject":
        purchase.status = "rejected"
        purchase.reject_reason = request.form.get("reason", "")
        db.session.commit()
        flash("تم رفض الفاتورة", "warning")

    return redirect(url_for("accountant_review"))


# ---------------------------------------------------------------
# التحويلات
# ---------------------------------------------------------------
@app.route("/transfers")
@login_required
def transfers_list():
    u = current_user()
    q = TransferOrder.query
    if u.role == "branch_staff":
        q = q.filter_by(to_location_id=u.location_id)
    elif u.role == "warehouse":
        q = q.filter_by(created_by=u.id)
    transfers = q.order_by(TransferOrder.created_at.desc()).all()
    return render_template("transfers_list.html", transfers=transfers)


@app.route("/transfers/new", methods=["GET", "POST"])
@login_required
@role_required("warehouse", "accountant")
def transfer_new():
    u = current_user()
    locations = Location.query.all()

    if request.method == "POST":
        to_location_id = int(request.form.get("to_location_id"))
        transfer = TransferOrder(
            from_location_id=u.location_id or locations[0].id,
            to_location_id=to_location_id,
            created_by=u.id,
            status="draft",
            attachments="[]",
        )
        db.session.add(transfer)
        db.session.commit()
        return redirect(url_for("transfer_edit", transfer_id=transfer.id))

    return render_template("transfer_new.html", locations=locations, user=u)


@app.route("/transfers/<int:transfer_id>", methods=["GET", "POST"])
@login_required
def transfer_edit(transfer_id):
    transfer = TransferOrder.query.get_or_404(transfer_id)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_line":
            code = request.form.get("code", "").strip()
            qty = float(request.form.get("quantity") or 0)
            ok, item, msg = vg.validate_line(code, qty, transfer.from_location_id)
            if not item:
                flash(msg, "error")
            else:
                line = TransferLine(
                    transfer_id=transfer.id, item_id=item.id,
                    qty_requested=qty, qty_sent=qty,
                )
                db.session.add(line)
                db.session.commit()

        elif action == "send":
            ok, errors = vg.validate_transfer_before_send(transfer)
            if not ok:
                for e in errors:
                    flash(e, "error")
            else:
                transfer.status = "in_transit"
                db.session.commit()
                flash("تم إرسال التحويل، بانتظار تأكيد استلام الفرع", "success")
                return redirect(url_for("transfers_list"))

        return redirect(url_for("transfer_edit", transfer_id=transfer.id))

    return render_template("transfer_edit.html", transfer=transfer)


@app.route("/transfers/<int:transfer_id>/receive", methods=["GET", "POST"])
@login_required
@role_required("branch_staff", "accountant")
def transfer_receive(transfer_id):
    transfer = TransferOrder.query.get_or_404(transfer_id)

    if request.method == "POST":
        has_variance = False
        for line in transfer.lines:
            field = f"qty_received_{line.id}"
            val = request.form.get(field)
            if val is not None and val != "":
                line.qty_received = float(val)
                if line.qty_received != line.qty_sent:
                    has_variance = True
                    line.variance_reason = request.form.get(f"reason_{line.id}", "")

        transfer.confirmed_by = current_user().id
        transfer.confirmed_at = datetime.utcnow()
        transfer.status = "needs_review" if has_variance else "received"
        db.session.commit()
        flash("تم تسجيل الاستلام" + (" — يوجد فرق يحتاج مراجعة المحاسب" if has_variance else ""), "success")
        return redirect(url_for("transfers_list"))

    return render_template("transfer_receive.html", transfer=transfer)


@app.route("/transfers/<int:transfer_id>/approve", methods=["POST"])
@login_required
@role_required("accountant")
def transfer_approve(transfer_id):
    transfer = TransferOrder.query.get_or_404(transfer_id)
    transfer.status = "queued"
    db.session.commit()
    sq.enqueue("transfer", transfer.id)
    flash("تم اعتماد التحويل وإدراجه في طابور الترحيل", "success")
    return redirect(url_for("accountant_review"))


# ---------------------------------------------------------------
# طلب صنف جديد
# ---------------------------------------------------------------
@app.route("/new-item-request", methods=["GET", "POST"])
@login_required
def new_item_request():
    if request.method == "POST":
        file = request.files.get("attachment")
        path = save_attachment(file, "new_items")
        req = NewItemRequest(
            requested_by=current_user().id,
            name_ar=request.form.get("name_ar"),
            name_en=request.form.get("name_en"),
            unit=request.form.get("unit"),
            conversion_factor=float(request.form.get("conversion_factor") or 1),
            estimated_cost=float(request.form.get("estimated_cost") or 0),
            supplier=request.form.get("supplier"),
            attachment_path=path,
            status="pending",
        )
        db.session.add(req)
        db.session.commit()
        flash("تم إرسال طلب الصنف الجديد للمحاسب للاعتماد", "success")
        return redirect(url_for("dashboard"))

    return render_template("new_item_request.html")


def generate_next_sku(prefix="sk"):
    """يولّد كوداً جديداً بنفس نمط الأكواد الحالية (مثال: sk-0767)."""
    existing = Item.query.filter(Item.sku.ilike(f"{prefix}-%")).all()
    max_num = 0
    for it in existing:
        try:
            num = int(it.sku.split("-")[1])
            max_num = max(max_num, num)
        except Exception:
            continue
    return f"{prefix}-{max_num + 1:04d}"


@app.route("/accountant/new-items/<int:req_id>/decide", methods=["POST"])
@login_required
@role_required("accountant")
def decide_new_item(req_id):
    req = NewItemRequest.query.get_or_404(req_id)
    action = request.form.get("action")

    if action == "approve":
        new_sku = generate_next_sku()
        item = Item(
            sku=new_sku,
            name_ar=req.name_ar,
            name_en=req.name_en,
            storage_unit=req.unit,
            ingredient_unit=req.unit,
            storage_to_ingredient_factor=req.conversion_factor,
            cost=req.estimated_cost,
            sync_status="pending",
        )
        db.session.add(item)
        req.status = "approved"
        req.approved_by = current_user().id
        req.foodics_sku = new_sku
        db.session.commit()
        sq.enqueue("new_item", req.id)
        flash(f"تم اعتماد الصنف وتوليد الكود: {new_sku}", "success")

    elif action == "reject":
        req.status = "rejected"
        req.reject_reason = request.form.get("reason", "")
        db.session.commit()
        flash("تم رفض طلب الصنف", "warning")

    return redirect(url_for("accountant_review"))


# ---------------------------------------------------------------
# الجرد
# ---------------------------------------------------------------
@app.route("/counts/new", methods=["GET", "POST"])
@login_required
def count_new():
    locations = Location.query.all()
    if request.method == "POST":
        location_id = int(request.form.get("location_id"))
        session_obj = CountSession(location_id=location_id, created_by=current_user().id, status="open")
        db.session.add(session_obj)
        db.session.commit()
        return redirect(url_for("count_edit", session_id=session_obj.id))
    return render_template("count_new.html", locations=locations)


@app.route("/counts/<int:session_id>", methods=["GET", "POST"])
@login_required
def count_edit(session_id):
    session_obj = CountSession.query.get_or_404(session_id)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_line":
            code = request.form.get("code", "").strip()
            counted = float(request.form.get("counted_quantity") or 0)
            item = vg.check_item_exists(code)
            if not item:
                flash("الصنف غير موجود محلياً", "error")
            else:
                line = CountLine(
                    session_id=session_obj.id, item_id=item.id,
                    book_quantity=0, counted_quantity=counted,
                )
                db.session.add(line)
                db.session.commit()
        elif action == "close_session":
            session_obj.status = "pending_review"
            db.session.commit()
            return redirect(url_for("dashboard"))

        return redirect(url_for("count_edit", session_id=session_obj.id))

    return render_template("count_edit.html", session_obj=session_obj)


# ---------------------------------------------------------------
# لوحة المحاسب (مراجعة واعتماد شاملة + الطابور)
# ---------------------------------------------------------------
@app.route("/accountant/review")
@login_required
@role_required("accountant")
def accountant_review():
    purchases = PurchaseTransaction.query.filter_by(status="submitted").all()
    transfers = TransferOrder.query.filter_by(status="needs_review").all()
    new_items = NewItemRequest.query.filter_by(status="pending").all()
    failed_jobs = db.session.query(SyncQueue, SyncLog).join(
        SyncLog, SyncQueue.id == SyncLog.queue_id
    ).filter(SyncQueue.status == "failed").all()
    return render_template(
        "accountant_review.html",
        purchases=purchases, transfers=transfers, new_items=new_items, failed_jobs=failed_jobs,
    )


@app.route("/sync/process", methods=["POST"])
@login_required
@role_required("accountant")
def sync_process():
    results = sq.process_all_queued()
    success_count = sum(1 for _, ok in results if ok)
    flash(f"تمت معالجة {len(results)} عملية، نجح منها {success_count}", "success")
    return redirect(url_for("accountant_review"))


@app.route("/sync/retry/<int:job_id>", methods=["POST"])
@login_required
@role_required("accountant")
def sync_retry(job_id):
    job = SyncQueue.query.get_or_404(job_id)
    job.status = "queued"
    job.retry_count = 0
    db.session.commit()
    sq.process_job(job)
    return redirect(url_for("accountant_review"))


# ---------------------------------------------------------------
# تقديم الملفات المرفوعة محلياً (بديل مؤقت لروابط Google Drive)
# ---------------------------------------------------------------
@app.route("/uploads/<path:filepath>")
def serve_upload(filepath):
    from flask import send_from_directory
    return send_from_directory(UPLOAD_DIR, filepath)


# التهيئة عند الإقلاع: تعمل مع gunicorn أيضاً وليس فقط عند التشغيل المباشر
with app.app_context():
    db.create_all()
    try:
        ensure_seed_data()
    except Exception as _seed_err:
        print(f"تحذير: تعذّر التحميل الأولي للبيانات: {_seed_err}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
