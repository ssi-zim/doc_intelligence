frappe.pages["di-copilot"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "Ask ERPNext",
        single_column: true,
    });
    page.add_action_item("History", () => frappe.set_route("List", "DI Copilot Log"));

    const root = $('<div id="di-cp-root" style="max-width:900px;margin:0 auto;padding:24px 0"></div>')
        .appendTo($(wrapper).find(".layout-main-section"));
    di_copilot.init(root[0]);
};

const di_copilot = {
    root: null,
    busy: false,

    init(root) {
        this.root = root;
        this.render_shell();
        this.load_demo_prompts();
    },

    render_shell() {
        $(this.root).html(`
            <p style="color:var(--text-muted);margin-bottom:14px">
                Ask a plain-English question about your ERPNext data. Every question runs through a
                validated query — no raw SQL, nothing is ever changed without your explicit approval.
            </p>
            <div style="display:flex;gap:8px;margin-bottom:10px">
                <input id="di-cp-input" type="text" class="form-control"
                       placeholder="e.g. Show unpaid invoices older than 30 days" />
                <button id="di-cp-ask" class="btn btn-primary">Ask</button>
            </div>
            <div id="di-cp-demos" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:20px"></div>
            <div id="di-cp-result"></div>
        `);
        const $input = $(this.root).find("#di-cp-input");
        $(this.root).find("#di-cp-ask").on("click", () => this.ask($input.val()));
        $input.on("keydown", (e) => { if (e.key === "Enter") this.ask($input.val()); });
    },

    load_demo_prompts() {
        frappe.call({
            method: "doc_intelligence.doc_intelligence.api.copilot_demo_prompts",
            callback: (r) => {
                const prompts = r.message || [];
                $(this.root).find("#di-cp-demos").html(prompts.map(p => `
                    <button class="btn btn-xs btn-default di-cp-demo" style="border-radius:14px">${frappe.utils.escape_html(p)}</button>
                `).join(""));
                $(this.root).find(".di-cp-demo").on("click", (e) => {
                    const q = $(e.currentTarget).text();
                    $(this.root).find("#di-cp-input").val(q);
                    this.ask(q);
                });
            },
        });
    },

    ask(question) {
        question = (question || "").trim();
        if (!question || this.busy) return;
        this.busy = true;
        const $result = $(this.root).find("#di-cp-result");
        $result.html(`<div class="text-muted"><i class="fa fa-spinner fa-spin"></i> Thinking…</div>`);
        frappe.call({
            method: "doc_intelligence.doc_intelligence.api.copilot_ask",
            args: { question },
            callback: (r) => {
                this.busy = false;
                if (r.message) this.render_result(question, r.message);
            },
            error: () => { this.busy = false; },
        });
    },

    render_result(question, res) {
        const $result = $(this.root).find("#di-cp-result");
        const note = res.note ? `<div style="color:var(--text-muted);margin-bottom:10px">${frappe.utils.escape_html(res.note)}</div>` : "";

        if (res.action === "unsupported") {
            $result.html(`${note}<div class="alert alert-warning">I can't answer that with the data available here.</div>`);
            return;
        }

        if (res.action === "count") {
            $result.html(`
                ${note}
                <div style="background:var(--card-bg);border:1px solid var(--border-color);border-radius:10px;padding:24px;text-align:center">
                    <div style="font-size:40px;font-weight:700;color:#2563eb">${res.total}</div>
                    <div style="color:var(--text-muted)">${frappe.utils.escape_html(res.doctype)}</div>
                </div>
            `);
            return;
        }

        if (res.action === "create_draft") {
            this.render_create_draft(res);
            return;
        }

        // list / get
        const rows = res.rows || [];
        const fields = res.fields || [];
        if (!rows.length) {
            $result.html(`${note}<div class="alert alert-info">No matching ${frappe.utils.escape_html(res.doctype)} records.</div>`);
            return;
        }
        const head = fields.map(f => `<th>${frappe.utils.escape_html(f)}</th>`).join("");
        const body = rows.map(row => {
            const cells = fields.map(f => {
                const val = row[f];
                if (f === "name") {
                    return `<td><a href="/app/${frappe.router.slug(res.doctype)}/${encodeURIComponent(val)}">${frappe.utils.escape_html(val)}</a></td>`;
                }
                return `<td>${val === null || val === undefined ? "" : frappe.utils.escape_html(String(val))}</td>`;
            }).join("");
            return `<tr>${cells}</tr>`;
        }).join("");
        $result.html(`
            ${note}
            <div style="color:var(--text-muted);font-size:12px;margin-bottom:6px">${res.count} result${res.count === 1 ? "" : "s"}</div>
            <div class="table-responsive">
                <table class="table table-bordered"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
            </div>
        `);
    },

    render_create_draft(res) {
        const $result = $(this.root).find("#di-cp-result");
        const values = res.values || {};
        const rows = Object.keys(values).map(k => `
            <tr><td style="font-weight:600;width:35%">${frappe.utils.escape_html(k)}</td>
                <td>${frappe.utils.escape_html(String(values[k]))}</td></tr>
        `).join("") || `<tr><td colspan="2" class="text-muted">No fields were filled in — you'll be able to complete the form after creating.</td></tr>`;
        $result.html(`
            <div style="color:var(--text-muted);margin-bottom:10px">${frappe.utils.escape_html(res.note || "")}</div>
            <div style="background:var(--card-bg);border:1px solid var(--border-color);border-radius:10px;padding:16px">
                <div style="font-weight:600;margin-bottom:10px">Proposed new ${frappe.utils.escape_html(res.doctype)} — nothing has been created yet</div>
                <table class="table table-bordered">${rows}</table>
                <div style="margin-top:10px;display:flex;gap:8px">
                    <button class="btn btn-primary btn-sm" id="di-cp-approve">Approve &amp; Create</button>
                    <button class="btn btn-default btn-sm" id="di-cp-cancel">Cancel</button>
                </div>
            </div>
        `);
        $(this.root).find("#di-cp-cancel").on("click", () => $result.html(""));
        $(this.root).find("#di-cp-approve").on("click", () => {
            frappe.call({
                method: "doc_intelligence.doc_intelligence.api.copilot_confirm_create",
                args: { doctype: res.doctype, values },
                callback: (r) => {
                    if (r.message) {
                        frappe.show_alert({ message: `Created ${r.message.doctype} ${r.message.name}`, indicator: "green" });
                        frappe.set_route(frappe.router.slug(r.message.doctype), r.message.name);
                    }
                },
            });
        });
    },
};
