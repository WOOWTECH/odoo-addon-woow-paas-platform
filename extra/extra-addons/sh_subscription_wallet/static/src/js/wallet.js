/** @odoo-module **/

document.addEventListener('DOMContentLoaded', function() {
    // 儲值金額選擇
    const topupOptions = document.querySelectorAll('.wo-topup-option');
    const customAmountInput = document.getElementById('custom_amount');
    const topupForm = document.getElementById('topup_form');

    if (topupOptions.length > 0) {
        topupOptions.forEach(function(option) {
            option.addEventListener('click', function() {
                // 移除其他選項的選中狀態
                topupOptions.forEach(function(opt) {
                    opt.classList.remove('wo-selected');
                    opt.querySelector('.topup-radio').checked = false;
                });

                // 選中當前選項
                this.classList.add('wo-selected');
                this.querySelector('.topup-radio').checked = true;

                // 清空自訂金額
                if (customAmountInput) {
                    customAmountInput.value = '';
                }
            });
        });
    }

    // 自訂金額輸入時取消選項選擇
    if (customAmountInput) {
        customAmountInput.addEventListener('input', function() {
            topupOptions.forEach(function(opt) {
                opt.classList.remove('wo-selected');
                opt.querySelector('.topup-radio').checked = false;
            });
        });
    }

    // 表單提交驗證
    if (topupForm) {
        topupForm.addEventListener('submit', function(e) {
            const selectedOption = document.querySelector('.topup-radio:checked');
            const customAmount = customAmountInput ? customAmountInput.value : '';

            if (!selectedOption && !customAmount) {
                e.preventDefault();
                alert('請選擇儲值金額或輸入自訂金額');
                return false;
            }

            if (customAmount && parseFloat(customAmount) < 100) {
                e.preventDefault();
                alert('最低儲值金額為 NT$ 100');
                return false;
            }

            // 如果有自訂金額，使用自訂金額
            if (customAmount) {
                // 建立隱藏欄位
                let hiddenInput = document.querySelector('input[name="amount"][type="hidden"]');
                if (!hiddenInput) {
                    hiddenInput = document.createElement('input');
                    hiddenInput.type = 'hidden';
                    hiddenInput.name = 'amount';
                    topupForm.appendChild(hiddenInput);
                }
                hiddenInput.value = customAmount;
            }

            // 顯示 loading
            const submitBtn = document.getElementById('topup_submit');
            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>處理中...';
            }
        });
    }
});
