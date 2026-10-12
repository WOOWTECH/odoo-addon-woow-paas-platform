# 含稅訂閱價格預估修補

實際隔離Odoo稅計算已重現：含稅單價100的原生total_included為100，平台helper卻回104.76。此修補限預估算術，不更動稅務政策、正式訂閱taxes、錢包或付款流程。既有PaaS seed點數訂閱免稅契約及production試用出現5%預估另行追查。

1. 在既有TestSkipTrialEstimate增加含稅總額／wallet sufficiency、月費估算、product-tax fallback回歸。
2. 真K8s own empty DB先跑原helper的RED；修補使用Odoo compute_all的total_excluded及total_included，不在輸入價格上再次加稅。
3. 同DB跑GREEN及既有tax-excluded／no-invoice／no-subscription-advance回歸，執行repo lint與manifest gate。
4. 分支及待覆核PR可審查；人工merge／production build gate，不改正式部署。

UI下單價格展示與使用者商用政策偏好不包含於本算術修補。
