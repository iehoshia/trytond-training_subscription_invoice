#This file is part of Tryton.  The COPYRIGHT file at the top level of
#this repository contains the full copyright notices and license terms.
from decimal import Decimal
from datetime import datetime, timedelta, date
import operator
from itertools import izip, groupby
from sql import Column, Literal
from sql.aggregate import Sum
from sql.conditionals import Coalesce

from trytond.model import Workflow, ModelView, ModelSQL, fields
from trytond.wizard import Wizard, StateView, StateAction, StateTransition, \
    Button
from trytond.report import Report
from trytond.tools import reduce_ids
from trytond.pyson import Eval, PYSONEncoder, Date, Id
from trytond.transaction import Transaction
from trytond.pool import Pool
from trytond import backend

STATES = {
    'readonly': (Eval('state') != 'draft'),
}

STATES_CONFIRMED = {
    'readonly': (Eval('state') != 'draft'),
    'required': (Eval('state') == 'confirmed'),
}

GUARANTEE = [
    ('payment', 'Payment'),
    ('voucher', 'Voucher'),
    ('credit_card', 'Credit Card'),
    ('letter', 'Letter'),
    ]

class TrainingCoursePendingReason(ModelView, ModelSQL):
    __name__ = 'training.course.pending.reason'

    _columns = {
        'code' : fields.char('Code', size=32, required=True),
        'name' : fields.char('Name', size=32, translate=True, required=True),
    }

    _sql_constraints = [
        ('uniq_code', 'unique(code)', "Must be unique"),
    ]

class TrainingCoursePending(ModelView, ModelSQL):
    'Training Course Pending'
    __name__ = 'training.course.pending'


    def _seance_next_date_compute(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, '') #time.strftime('%Y-%m-%d %H:%M:%S'))

        for obj in self.browse(cr, uid, ids, context=context):
            cr.execute("""
                       SELECT MIN(sea.date)
                       FROM training_session_seance_rel tssr, training_seance sea, training_session ses
                       WHERE sea.id = tssr.seance_id
                       AND ses.id = tssr.session_id
                       AND sea.date >= %s
                       AND ses.STATE IN ('opened', 'opened_confirmed', 'inprogress', 'closed_confirmed')
                       AND sea.course_id = %s
                       """, (datetime.now()), obj.course_id.id)
            values = cr.fetchone()
            value = values[0]

            if value:
                res[obj.id] = value

        return res

    _columns = {
        'followup_by' : fields.many2one('res.users', 'Followup By', required=True, select=1),
        'course_id' : fields.many2one('training.course', 'Course', select=1, required=True),
        'type_id' : fields.related('course_id', 'course_type_id', type='many2one', relation='training.course_type',  string='Type'),
        'category_id' : fields.related('course_id', 'category_id', type='many2one', relation='training.course_category',  string='Category'),
        'lang_id' : fields.related('course_id', 'lang_id', type='many2one', relation='res.lang', string='Language'),
        'state' : fields.related('course_id', 'state_course',
                                 type='selection',
                                 selection=[('draft', 'Draft'),
                                            ('pending', 'Pending'),
                                            ('inprogress', 'In Progress'),
                                            ('deprecated', 'Deprecated'),
                                            ('validated', 'Validated'),
                                           ],
                                 select=1,
                                 string='State',
                                ),
        'type' : fields.selection(training_course_pending_reason_compute,
                                    'Reason',
                                    size=32,
                                    select=1),
        'date' : fields.date('Planned Date'),
        'reason' : fields.text('Note'),
        'purchase_order_id' : fields.many2one('purchase.order', 'Purchase Order'),
        'create_date' : fields.datetime('Create Date', readonly=True),
        'job_id' : fields.many2one('res.partner.job', 'Contact', required=True),
        'job_email' : fields.char('Email', size=64),
        'seance_next_date' : fields.function(_seance_next_date_compute,
                                             method=True,
                                             string='Seance Next Date',
                                             type='datetime'),
        'todo' : fields.boolean('Todo'),
    }

    def on_change_job(self, cr, uid, ids, job_id, context=None):
        if not job_id:
            return False

        job = self.browse(cr, uid, job_id, context=context)
        return {
            'value' : {
                'job_email' : job.email
            }
        }

    def action_open_course(self, cr, uid, ids, context=None):
        this = self.browse(cr, uid, ids[0], context=context)

        res = {
            'view_type': 'form',
            "view_mode": 'form',
            'res_model': 'training.course',
            'view_id': self.pool.get('ir.ui.view').search(cr,uid,[('name','=','training.course.form')]),
            'type': 'ir.actions.act_window',
            'target': 'current',
            'res_id' : this.course_id.id,
        }
        return res

    def action_validate_course(self, cr, uid, ids, context=None):
        this = self.browse(cr, uid, ids[0], context=context)

        workflow = netsvc.LocalService("workflow")
        return workflow.trg_validate(uid, 'training.course', this.course_id.id, 'signal_validate', cr)

    _defaults = {
        'todo' : lambda *a: 0,
        'followup_by' : lambda obj, cr, uid, context: uid,
    }

class TrainingContantCourse(ModelView, ModelSQL):
    _name = 'training.contact.course'

    _columns = {
        'function' : fields.char('Function', size=64, readonly=True),
        'course_id' : fields.many2one('training.course', 'Course', readonly=True),
        'contact_id' : fields.many2one('res.partner.contact', 'Contact', readonly=True),
    }

    def init(self, cr):
        tools.drop_view_if_exists(cr, 'training_contact_course')
        cr.execute("CREATE OR REPLACE VIEW training_contact_course as ( "
                   "SELECT job.id, function, rel.course_id, rel.job_id, job.contact_id "
                   "FROM training_course_job_rel rel, (SELECT id, contact_id, function FROM res_partner_job) AS job "
                   "WHERE job.id = rel.job_id )")

class ResPartnerContact(ModelView, ModelSQL):
    __name__ = 'res.partner.contact'

    courses = fields.One2Many('training.contact.course', 'contact', 'Courses', readonly=True)    

class training_session_duplicate_wizard(ModelView, ModelSQL, Workflow):
    _name = 'training.session.duplicate.wizard'

    _columns = {
        'session_id': fields.many2one('training.session', 'Session',
                                      required=True,
                                      readonly=True,
                                      domain=[('state', 'in', ['opened', 'opened_confirmed'])]),
        'group_id' : fields.many2one('training.group', 'Group',
                                     domain="[('session_id', '=', session_id)]"),
        'subscription_line_ids' : fields.many2many('training.subscription.line',
                                                   'training_sdw_participation_rel',
                                                   'wizard_id',
                                                   'participation_id',
                                                   'Participations',
                                                   domain="[('session_id', '=', session_id),('state', '=', 'confirmed')]"),
    }

    def action_cancel(self, cr, uid, ids, context=None):
        return {'type' : 'ir.actions.act_window_close'}

    def action_apply(self, cr, uid, ids, context=None):
        this = self.browse(cr, uid, ids[0], context=context)

        if len(this.subscription_line_ids) == 0:
            raise osv.except_osv(_('Error'),
                                 _('You have not selected a participant of this session'))

        seances = []

        if any(len(seance.session_ids) > 1 for seance in this.session_id.seance_ids):
            raise osv.except_osv(_('Error'),
                                 _('You have selected a session with a shared seance'))

        #if not all(seance.state == 'opened' for seance in this.session_id.seance_ids):
        #    raise osv.except_osv(_('Error'),
        #                         _('You have to open all seances in this session'))

        lengths = [len(group.seance_ids)
                   for group in this.session_id.group_ids
                   if group != this.group_id]

        if len(lengths) == 0:
            raise osv.except_osv(_('Error'),
                                 _('There is no group in this session !'))

        minimum, maximum = min(lengths), max(lengths)

        if minimum != maximum:
            raise osv.except_osv(_('Error'),
                                 _('The defined groups for this session does not have the same number of seances !'))

        group_id = this.session_id.group_ids[0]

        seance_sisters = {}
        for group in this.session_id.group_ids:
            for seance in group.seance_ids:
                seance_sisters.setdefault((seance.date, seance.duration, seance.course_id, seance.kind,), {})[seance.id] = None

        seance_ids = []

        if len(this.group_id.seance_ids) == 0:
            proxy_seance = self.pool.get('training.seance')

            for seance in group_id.seance_ids:
                values = {
                    'group_id' : this.group_id.id,
                    'presence_form' : 'no',
                    'manual' : 0,
                    'participant_count_manual' : 0,
                    'contact_ids' : [(6, 0, [])],
                    'participant_ids' : [],
                    'duplicata' : 1,
                    'duplicated' : 1,
                    'is_first_seance' : seance.is_first_seance,
                }

                seance_ids.append( proxy_seance.copy(cr, uid, seance.id, values, context=context) )
        else:
            # If the there are some seances in this group
            seance_ids = [seance.id for seance in this.group_id.seance_ids]

        for seance in self.pool.get('training.seance').browse(cr, uid, seance_ids, context=context):
            key = (seance.date, seance.duration, seance.course_id, seance.kind,)
            if key in seance_sisters:
                for k, v in seance_sisters[key].items():
                    seance_sisters[key][k] = seance.id
            else:
                seance_sisters[key][seance.id] = seance.id

        final_mapping = {}
        for key, values in seance_sisters.iteritems():
            for old_seance_id, new_seance_id in values.iteritems():
                final_mapping[old_seance_id] = new_seance_id

        for sl in this.subscription_line_ids:
            for part in sl.participation_ids:
                part.write({'seance_id' : final_mapping[part.seance_id.id]})

        return {'type' : 'ir.actions.act_window_close'}

    def default_get(self, cr, uid, fields, context=None):
        record_id = context and context.get('record_id', False) or False

        res = super(training_session_duplicate_wizard, self).default_get(cr, uid, fields, context=context)

        if record_id:
            res['session_id'] = record_id

        return res

class TrainingConfigProduct(ModelView,ModelSQL):
    'Training Config Product'
    __name__ = 'training.config.product'

    type = fields.Selection(
            [
                ('support_of_course', 'Support of Course'),
                ('voucher', 'Voucher'),
            ],
            'Type',
            required=True)
    product_id = fields.Many2One('product.product', 'Product', required=True)

    @classmethod
    def __setup__(cls):
        super(TrainingConfigProduct, cls).__setup__()
        cls._sql_constraints += [
            ('uniq_type_product', 'UNIQUE(type, product)', 'You can not assign twice the same product to the same type.')]

class TrainingConfigPenalty(ModelView, ModelSQL):
    'Training Config Penalty'
    __name__ = 'training.config.penality'

    trigger = fields.selection(
            [
                ('discount_refund', 'Discount Refund'),
                ('discount_invoice', 'Discount Invoice'),
            ],
            'Trigger',
            required=True,
        )
    rate = fields.float('Rate')

    def _check_value(self, cr, uid, ids, context=None):
        return all(obj.rate >= 0.0 and obj.rate <= 100.0 for obj in self.browse(cr, uid, ids, context=context))

    _constraints = [
        (_check_value, "You can not have a value lesser than 0.0 !", ['rate'])
    ]

    @classmethod
    def __setup__(cls):
        super(TrainingConfigProduct, cls).__setup__()
        cls._sql_constraints += [
            ('uniq_trigger', 'UNIQUE(trigger)', 'You can not define twice the same trigger !')]

class TrainingConfigInvoice(ModelView, ModelSQL):
    'Training Config Invoice'
    __name__ = 'training.config.invoice'

    threshold = fields.Selection(
            [
                ('minimum', 'Minimum'),
                ('maximum', 'Maximum'),
            ],
            'Threshold',
            required=True
        )
    price = fields.Integer('Price')

    def _check_value(self, cr, uid, ids, context=None):
        return all(obj.price >= 0.0 for obj in self.browse(cr, uid, ids, context=context))

    _constraints = [
        (_check_value, "You can not have a value lesser than 0.0 !", ['price'])
    ]

    _sql_constraints = [
        ('uniq_threshold', 'unique(threshold)', "You can not define twice the same threshold !"),
    ]