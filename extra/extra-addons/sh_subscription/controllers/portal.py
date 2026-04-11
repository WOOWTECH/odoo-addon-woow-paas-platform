# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager
from dateutil.relativedelta import relativedelta

class SHSubscriptionPortal(CustomerPortal):

    # Prepare the portal home page values
    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        subscription_count = request.env['sh.subscription.subscription'].search_count(
            [('sh_partner_id', '=', request.env.user.partner_id.id)]
        )
        if 'subscription_count' in counters:
            values.update({'subscription_count': subscription_count})
        return values

    # Route to display user's subscriptions with pagination
    @http.route(['/my/sh_subscription', '/my/sh_subscription/page/<int:page>'], type='http', auth="user")
    def portal_my_home_subscription(self, page=1):
        values = self._prepare_portal_layout_values()
        subscription_obj = request.env['sh.subscription.subscription']
        domain = [('sh_partner_id', '=', request.env.user.partner_id.id)]

        # Count subscriptions for pagination
        subscription_count = subscription_obj.sudo().search_count(domain)
        pager = portal_pager(
            url="/my/sh_subscription",
            total=subscription_count,
            page=page,
            step=self._items_per_page
        )

        subscriptions = subscription_obj.sudo().search(
            domain, limit=self._items_per_page, offset=pager['offset']
        )

        values.update({
            'subscriptions': subscriptions,
            'page_name': 'sh_subscription',
            'pager': pager,
            'default_url': '/my/sh_subscription',
            'subscription_count': subscription_count,
        })
        return request.render("sh_subscription.sh_portal_my_subscriptions", values)

    # Route to display a specific subscription's details
    @http.route(['/my/sh_subscription/<int:subscription_id>'], type='http', auth="user")
    def portal_my_sh_subscription_form(self, subscription_id, message=False, **kw):
        sh_subscription = request.env['sh.subscription.subscription'].sudo().browse(subscription_id)
        if not sh_subscription or sh_subscription.sh_partner_id.id != request.env.user.partner_id.id:
            return request.redirect('/my')

        values = {
            'sh_subscription': sh_subscription,
            'message': message,
            'bootstrap_formatting': True,
            'sh_partner_id': sh_subscription.sh_partner_id.id,
        }
        return request.render('sh_subscription.sh_subscription_portal_content', values)

    # Route to cancel a subscription
    @http.route('/cancel-subscription', type="json", auth="user")
    def portal_cancel_subscription(self, **kw):
        response = {}
        try:
            subscription_id = request.env['sh.subscription.subscription'].sudo().browse(
                int(kw.get('subscription_id', 0))
            )
            # Ownership check: only the subscription's partner can cancel
            if not subscription_id.exists() or subscription_id.sh_partner_id.id != request.env.user.partner_id.id:
                response.update({'error': 'Access denied'})
                return response
            if subscription_id and kw.get('sh_reason_id') and kw.get('description'):
                reason_id = request.env['sh.subscription.reason'].sudo().browse(int(kw['sh_reason_id']))
                if reason_id:
                    subscription_id.sh_reason = f"{reason_id.name} {kw['description']}"
                    subscription_id.state = 'cancel' if subscription_id.state == 'draft' else 'close'
                    subscription_id._sh_send_subscription_email(False)
                    response.update({'reload': True})
            else:
                response.update({'required': True})
        except Exception as e:
            response.update({'error': str(e)})
        return response

    # Route to renew a subscription
    @http.route('/renew-subscription', type="json", auth="user")
    def portal_renew_subscription(self, **kw):

        response = {}
        try:
            subscription = request.env['sh.subscription.subscription'].sudo().browse(
                int(kw.get('subscription_id', 0))
            )
            # Ownership check: only the subscription's partner can renew
            if not subscription.exists() or subscription.sh_partner_id.id != request.env.user.partner_id.id:
                response.update({'error': 'Access denied'})
                return response
            if subscription:
                date_value = subscription.sh_end_date + relativedelta(days=1) if subscription.state == 'in_progress' else fields.Date.today()
                subscription.sh_renew_stage = 'not_time_to_renew'
                subscription.state = 'renewed' if subscription.state != 'in_progress' else subscription.state
                subscription.sh_renewed = True

                sub_vals = {
                    'sh_partner_id': subscription.sh_partner_id.id,
                    'product_id': subscription.product_id.id,
                    'sh_partner_invoice_id': subscription.sh_partner_invoice_id.id,
                    'sh_taxes_ids': subscription.sh_taxes_ids.ids,
                    'sh_qty': subscription.sh_qty,
                    'sh_subscription_plan_id': subscription.sh_subscription_plan_id.id,
                    'sh_plan_price': subscription.product_id.lst_price if subscription.sh_subscription_plan_id.sh_override_product else subscription.sh_plan_price,
                    'sh_recurrency': subscription.sh_recurrency,
                    'sh_unit': subscription.sh_unit,
                    'sh_start_date': date_value,
                    'sh_no_of_billing_cycle': subscription.sh_no_of_billing_cycle,
                    'sh_source': subscription.sh_source,
                    'sh_order_ref_id': False,
                    'sh_subscription_ref': subscription.sh_subscription_ref,
                    'sh_subscription_id': subscription.id,
                    'sh_date_of_next_payment': date_value,
                }

                new_subscription = request.env["sh.subscription.subscription"].sudo().create(sub_vals)
                new_subscription._onchange_sh_trial_subcription_start_date()
                new_subscription._onchange_sh_trial_subcription_end_date()
                subscription._sh_send_subscription_email(False)
                response.update({'reload': True})
        except Exception as e:
            response.update({'error': str(e)})
        return response
