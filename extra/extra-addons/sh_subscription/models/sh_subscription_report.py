# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo import models, fields, tools
from psycopg2 import OperationalError

class SubscriptionReport(models.Model):
    _name = "sh.subscription.report"
    _auto = False
    _rec_name = 'id'
    _order = 'id desc'

    # Fields
    sh_month_price = fields.Float(string="Monthly Recurring Revenue")
    sh_partner_id = fields.Many2one(comodel_name="res.partner", string="Customer Name")
    sh_subscription_plan_id = fields.Many2one(comodel_name="sh.subscription.plan", string="Subscription Plan")
    sh_sale_person = fields.Many2one(comodel_name="res.users", string="Sales Person")

    # Method to select columns
    def _select(self):
        return '''
            s.id as id,
            s.sh_partner_id as sh_partner_id,
            SUM(COALESCE(s.sh_recurring_monthly, 0)) as sh_month_price,
            s.sh_subscription_plan_id as sh_subscription_plan_id,
            s.create_uid as sh_sale_person
        '''

    # Method to specify table name
    def _from(self):
        return 'sh_subscription_subscription AS s'

    # Method to define where conditions
    def _where(self):
        return 's.id > 0'

    # Method to group by columns
    def _group_by(self):
        return 's.id, s.sh_partner_id, s.sh_subscription_plan_id, s.create_uid'

    # Method to build the query string
    def _query(self):
        return f'(SELECT {self._select()} FROM {self._from()} WHERE {self._where()} GROUP BY {self._group_by()})'

    # Initialization method to create or replace SQL view
    def init(self):
        tools.drop_view_if_exists(self._cr, self._table)

        # Retry mechanism to handle database operational errors
        for attempt in range(3):  # Attempt 3 times
            try:
                self._cr.execute(f"CREATE OR REPLACE VIEW {self._table} AS {self._query()}")
                break  # Exit loop if successful
            except OperationalError as e:
                if attempt < 2:
                    continue  # Retry on operational error
                else:
                    raise e  # Raise error after final attempt
            except Exception as e:
                raise e  # Raise any other exceptions immediately
