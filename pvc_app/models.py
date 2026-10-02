import json
from datetime import datetime
from sqlalchemy import event
from flask_login import UserMixin
from . import db, login_manager


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(200))
    email = db.Column(db.String(200), unique=True)
    contact_no = db.Column(db.String(20))
    designation = db.Column(db.String(120))
    control_no = db.Column(db.String(120))
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    code = db.Column(db.String(50), unique=True)
    description = db.Column(db.Text)
    pvc_formula_code = db.Column(db.String(50), nullable=False)
    weights_json = db.Column(db.Text, default="{}")
    extra_fields_json = db.Column(db.Text, default="[]")

    @property
    def weights(self):
        return json.loads(self.weights_json or "{}")

    @property
    def extra_fields(self):
        return json.loads(self.extra_fields_json or "[]")


class ItemIndex(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False)
    item = db.relationship("Item")
    month = db.Column(db.Date, nullable=False)
    indices_json = db.Column(db.Text, nullable=False)


class PVCResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    user = db.relationship("User")
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False)
    item = db.relationship("Item")
    username = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    basicrate = db.Column(db.Float(precision=53))
    quantity = db.Column(db.Float(precision=53))
    freightrateperunit = db.Column(db.Float(precision=53))
    pvcbasedate = db.Column(db.String(10))
    origdp = db.Column(db.String(10))
    refixeddp = db.Column(db.String(10))
    extendeddp = db.Column(db.String(10))
    caldate = db.Column(db.String(10))
    supdate = db.Column(db.String(10))
    rateapplied = db.Column(db.String(100))
    pvcactual = db.Column(db.Float(precision=53))
    pvccontractual = db.Column(db.Float(precision=53))
    loweractual = db.Column(db.Float(precision=53))
    lowercontractual = db.Column(db.Float(precision=53))
    ldamtactual = db.Column(db.Float(precision=53))
    ldamtcontractual = db.Column(db.Float(precision=53))
    fairprice = db.Column(db.Float(precision=53))
    selectedscenario = db.Column(db.String(10))
    pvcactuallessldnew = db.Column(db.Float(precision=53))
    pvccontractuallessldnew = db.Column(db.Float(precision=53))
    loweractuallessld = db.Column(db.Float(precision=53))
    lowercontractuallessld = db.Column(db.Float(precision=53))
    delaydays = db.Column(db.Integer)
    ldweeksnew = db.Column(db.Integer)
    ldratepctnew = db.Column(db.Float(precision=53))
    ldapplicable = db.Column(db.Boolean, default=False)
    pvcperseta2 = db.Column(db.Float(precision=53))
    pvcpersetb2 = db.Column(db.Float(precision=53))
    pvcpersetc1 = db.Column(db.Float(precision=53))
    pvcpersetd1 = db.Column(db.Float(precision=53))
    tenderno = db.Column(db.String(100))
    pono = db.Column(db.String(100))
    scenarioamounts_json = db.Column(db.Text)
    scenariodetails_json = db.Column(db.Text)
    igbt_vendor_details_json = db.Column(db.Text)


class TenderMaster(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False)
    item = db.relationship("Item")
    tender_no = db.Column(db.String(100), unique=True, nullable=False)
    basicrate = db.Column(db.Float(precision=53), default=0)
    pvcbasedate = db.Column(db.String(10))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TenderVendor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tender_id = db.Column(db.Integer, db.ForeignKey("tender_master.id"), nullable=False)
    tender = db.relationship("TenderMaster", backref="vendors")
    po_no = db.Column(db.String(50))
    vendor_name = db.Column(db.String(200), nullable=False)
    cif = db.Column(db.Float(precision=53), default=0)
    freightrateperunit = db.Column(db.Float(precision=53), default=0)
    currency = db.Column(db.String(10), nullable=False)


# ── Audit Log ─────────────────────────────────────────────────────────────

class AuditLog(db.Model):
    __tablename__ = 'audit_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    user = db.relationship('User')
    action = db.Column(db.String(50), nullable=False)
    table_name = db.Column(db.String(100), nullable=False)
    row_id = db.Column(db.String(100))
    old_data = db.Column(db.Text)
    new_data = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

def _default_serializer(obj):
    from datetime import datetime, date
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)

def _get_current_user_id():
    from flask import has_request_context
    from flask_login import current_user
    if has_request_context() and current_user and current_user.is_authenticated:
        return current_user.id
    return None

@event.listens_for(db.Model, 'after_insert', propagate=True)
def audit_after_insert(mapper, connection, target):
    if isinstance(target, AuditLog):
        return
    
    new_data = {attr.key: getattr(target, attr.key) for attr in mapper.column_attrs}
    connection.execute(
        AuditLog.__table__.insert(),
        {
            'user_id': _get_current_user_id(),
            'action': 'INSERT',
            'table_name': target.__tablename__,
            'row_id': str(target.id) if hasattr(target, 'id') else None,
            'old_data': None,
            'new_data': json.dumps(new_data, default=_default_serializer),
            'timestamp': datetime.utcnow()
        }
    )

@event.listens_for(db.Model, 'after_update', propagate=True)
def audit_after_update(mapper, connection, target):
    if isinstance(target, AuditLog):
        return
        
    from sqlalchemy.orm.attributes import get_history
    old_data = {}
    new_data = {}
    for attr in mapper.column_attrs:
        prop = attr.key
        hist = get_history(target, prop)
        if hist.has_changes():
            old_data[prop] = hist.deleted[0] if hist.deleted else None
            new_data[prop] = hist.added[0] if hist.added else None
            
    if old_data or new_data:
        connection.execute(
            AuditLog.__table__.insert(),
            {
                'user_id': _get_current_user_id(),
                'action': 'UPDATE',
                'table_name': target.__tablename__,
                'row_id': str(target.id) if hasattr(target, 'id') else None,
                'old_data': json.dumps(old_data, default=_default_serializer),
                'new_data': json.dumps(new_data, default=_default_serializer),
                'timestamp': datetime.utcnow()
            }
        )

@event.listens_for(db.Model, 'after_delete', propagate=True)
def audit_after_delete(mapper, connection, target):
    if isinstance(target, AuditLog):
        return
        
    old_data = {attr.key: getattr(target, attr.key) for attr in mapper.column_attrs}
    connection.execute(
        AuditLog.__table__.insert(),
        {
            'user_id': _get_current_user_id(),
            'action': 'DELETE',
            'table_name': target.__tablename__,
            'row_id': str(target.id) if hasattr(target, 'id') else None,
            'old_data': json.dumps(old_data, default=_default_serializer),
            'new_data': None,
            'timestamp': datetime.utcnow()
        }
    )