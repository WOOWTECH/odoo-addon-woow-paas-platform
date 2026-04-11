import publicWidget from "@web/legacy/js/public/public_widget";
import { rpc } from "@web/core/network/rpc";

publicWidget.registry.SHSubscriptionWidget = publicWidget.Widget.extend({
    selector: '.sh_subscription_portal', // Update the selector to match the common parent container
    events: {
        'click .sh_cancel_btn,.sh_close_btn': '_onCancelBtnClick',
        'click .sh_cancel_now_cls': '_onCancelNowClick',
        'click .sh_renew_btn': '_renewSubscription',
    },


    // // Method to handle 'Cancel' button click in the form view
    _onCancelBtnClick: function () {
        $('.cancel_subscription_id').val($('.subscription_id').val());
        $('#cancel_subscription_modal').modal('show');
    },

    // Method to handle 'Cancel Now' button click in the modal
    _onCancelNowClick: async function (ev) {
        await rpc(
            '/cancel-subscription',
            { subscription_id: $('.cancel_subscription_id').val(),
                description: $('#sh_description').val(),
                sh_reason_id: $('#sh_reason_id').val() }
            ).then((result) => {
                var datas = JSON.parse(result);
                if (datas.required) {
                    alert("Reason is required");
                }
                if (datas.reload) {
                    location.reload(true);
                }
            console.log("Cancelling result", result);
        });
        
    },

    // Method to handle subscription renewal
    _renewSubscription: async function (ev) {
        await rpc(
            '/renew-subscription',
            { 
                subscription_id: $('#subscription_id').val(),
            }
            ).then((result) => {
            var datas = JSON.parse(result);
            if (datas.required) {
                alert("Reason is required");
            }
            if (datas.reload) {
                location.reload(true);
            }
        });
    },
});
