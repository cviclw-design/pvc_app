# pvc_app/views.py

from flask import Blueprint, render_template, request
from flask_login import login_required, current_user

from .models import Item, TenderMaster, PVCResult

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    return render_template("dashboard.html")


@main_bp.route("/history")
@login_required
def history():
    page = request.args.get("page", 1, type=int)
    query = PVCResult.query

    # If the user is not an admin, only show their own history.
    if not current_user.is_admin:
        query = query.filter_by(user_id=current_user.id)

    pagination = query.order_by(PVCResult.created_at.desc()).paginate(
        page=page, per_page=25, error_out=False
    )
    return render_template("history.html", records=pagination.items, pagination=pagination)