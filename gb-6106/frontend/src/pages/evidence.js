import { api } from "../api/workbench.js";
import { errorText, field } from "../components/common/fields.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { resultTable } from "../components/common/table.js";
import { submit } from "../hooks/submit.js";
import { el } from "../utils/dom.js";
import { cell, download, time } from "../utils/format.js";

function reviewForm(id, saved) {
  const form = el("form", {id: "review-form"},
    field("复核意见", el("textarea", {name: "note", required: true, minlength: 3, maxlength: 500, rows: 3})),
    errorText(), el("button", {type: "submit"}, "签署复核"));
  submit(form, async values => saved(await api.submitReview(id, {note: values.get("note")})));
  return form;
}

function signature(review) {
  return el("div", {"data-testid": "reviewed", class: "signature"},
    el("strong", {}, `已签署 · ${review.actor}`), el("p", {}, review.note), el("time", {}, time(review.created_at)));
}

function verificationPanel(run, user, issue) {
  const box = el("div", {class: "stack", "data-testid": "credential-verification"});
  const canRepair = user.role === "operator" || user.role === "reviewer";

  async function render() {
    let verification = null;
    let repairs = [];
    try {
      [verification, repairs] = await Promise.all([
        api.verification(run.id),
        api.repairs(run.id).catch(() => []),
      ]);
    } catch (error) {
      issue.textContent = error.message;
    }

    const bad = Boolean(verification && verification.download_allowed === false);
    const valid = Boolean(verification?.valid);
    const latestBad = repairs?.[0];
    const status = el(
      "p",
      {
        "data-testid": "verification-status",
        "data-status": verification?.status || "unknown",
        role: "status",
      },
      bad ? "验单不通过：凭据与完工指纹不一致，该批次已停发。"
        : valid ? "验单通过：凭据与完工指纹一致，可下载。"
        : "尚未完成验单；每次下载前仍会自动核对。",
    );

    const verifyButton = el("button", {
      type: "button",
      "data-testid": "verify-credential",
      onclick: async event => {
        event.currentTarget.disabled = true;
        issue.textContent = "";
        try {
          verification = await api.verify(run.id);
          await render();
        } catch (error) {
          issue.textContent = error.message;
          event.currentTarget.disabled = false;
        }
      },
    }, "验单");

    const actions = el("div", {class: "actions"}, verifyButton);
    if (bad && canRepair) {
      actions.append(el("button", {
        type: "button",
        "data-testid": "repair-credential",
        onclick: async event => {
          event.currentTarget.disabled = true;
          issue.textContent = "";
          try {
            const key = globalThis.crypto?.randomUUID?.() || `repair-${run.id}-${Date.now()}`;
            await api.repair(run.id, {idempotencyKey: key});
            window.dispatchEvent(new HashChangeEvent("hashchange"));
          } catch (error) {
            issue.textContent = error.message;
            event.currentTarget.disabled = false;
            await render();
          }
        },
      }, "修复"));
    } else if (bad) {
      actions.append(empty("等待资料操作员或复核员修复。"));
    }

    const details = [];
    if (verification?.expected_digest) {
      details.push(el("p", {class: "help"}, `完工指纹：${verification.expected_digest}`));
    }
    if (bad && verification?.actual_digest) {
      details.push(el("p", {class: "help"}, `凭据指纹：${verification.actual_digest}`));
    }
    if (latestBad?.bad_credential) {
      details.push(
        el("details", {},
          el("summary", {}, "查看留底的坏凭据"),
          el("pre", {class: "credential-archive"}, JSON.stringify(latestBad.bad_credential, null, 2)),
        ),
      );
    }
    if (verification?.repair_count) {
      details.push(el("p", {"data-testid": "repair-count"}, `已修复 ${verification.repair_count} 次；同一份坏凭据的并发提交只会修成一次。`));
    }

    box.replaceChildren(status, actions, ...details);
  }

  box.refresh = render;
  render();
  return box;
}

export async function evidencePage(config, user, route) {
  const runs = (await api.runs()).filter(item => item.status === "done");
  const run = route.id ? await api.run(route.id) : runs[0];
  if (!run?.result) return el("div", {class: "page"},
    pageHeading(config.modules[3], null), empty("成功执行一个批次后可以查看、签核和下载证据。"),
    el("a", {href: "#runs"}, config.modules[2]));

  const review = await api.review(run.id);
  const issue = errorText();
  const verificationPanelRef = verificationPanel(run, user, issue);
  async function onDownload(event) {
    const button = event.currentTarget;
    button.disabled = true;
    issue.textContent = "";
    try {
      await download(await api.export(run.id), `evidence-${run.id}.json`);
    } catch (error) {
      issue.textContent = error.message;
      await verificationPanelRef.refresh?.();
    } finally {
      button.disabled = false;
    }
  }
  const button = el("button", {type: "button", "data-testid": "export-evidence", onclick: onDownload}, "导出证据");
  const downloadButton = el("button", {type: "button", "data-testid": "download-evidence", title: "下载 JSON 凭据", onclick: onDownload}, "下载");
  const downloadActions = el("div", {class: "actions"}, button, downloadButton);
  const signing = el("div", {});
  signing.append(review ? signature(review) : user.role === "reviewer" ?
    reviewForm(run.id, data => signing.replaceChildren(signature(data))) : empty("待复核员签署。"));
  const options = el("select", {"aria-label": "选择已完成的批次", onchange: event => {
    location.hash = `evidence/${event.target.value}`;
  }}, runs.map(item => el("option", {value: item.id, selected: item.id === run.id}, `#${item.id} ${item.case_title}`)));
  return el("div", {class: "page evidence"},
    pageHeading(config.modules[3], run.case_title, downloadActions),
    issue, options,
    el("section", {class: "feature evidence-summary"}, el("div", {}, el("h2", {}, "批次判定"), el("p", {}, run.result.summary)),
      el("details", {}, el("summary", {}, "证据摘要"), el("code", {}, run.digest))),
    el("dl", {class: "metrics"}, Object.entries(run.result.metrics).map(([label, value]) =>
      el("div", {}, el("dt", {}, label), el("dd", {}, cell(value))))),
    section("业务结果明细", run.result.rows?.length ? resultTable(run.result.rows) : empty("此批次无明细。")),
    section("验单与修复 · 追溯地址表", verificationPanelRef),
    section("复核签署", signing));
}
