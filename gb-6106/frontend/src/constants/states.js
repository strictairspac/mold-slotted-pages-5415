export const States = Object.freeze({
  queued: "等待执行", done: "已完成", failed: "执行失败",
});

export const Actions = Object.freeze({
  create_case: "登记资料", enqueue: "提交执行", done: "执行完成", failed: "执行失败", review: "复核签署",
  verify_credential: "验单", credential_detained: "坏凭据留底", repair: "修复凭据",
  repair_failed: "修复失败", repair_fingerprint_mismatch: "指纹不符", credential_recovery: "中断恢复",
});
