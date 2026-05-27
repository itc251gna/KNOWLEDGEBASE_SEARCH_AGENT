(function () {
    const FILTERS = [
        ["pages", "Pages"],
        ["applications", "Applications"],
        ["links", "Links"],
        ["pdf", "PDF"],
        ["word", "Word"],
        ["excel", "Excel"],
        ["powerpoint", "PowerPoint"],
    ];
    const SOURCE_SCOPES = [
        ["portal", "Portal only"],
        ["all", "All knowledge base"],
        ["filesystem", "Files only"],
    ];

    function text(value) {
        return value || "";
    }

    function escapeHtml(value) {
        return text(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function highlighted(value) {
        return escapeHtml(value)
            .replace(/&lt;mark&gt;/g, "<mark>")
            .replace(/&lt;\/mark&gt;/g, "</mark>");
    }

    function selectedFilters(root) {
        return Array.from(root.querySelectorAll("[data-search-filter]:checked")).map(item => item.value);
    }

    function selectedSourceScope(root, fallback) {
        const selected = root.querySelector("[data-search-source]:checked");
        return selected ? selected.value : (fallback || "portal");
    }

    function renderFilters(root) {
        const filterNode = root.querySelector("[data-search-filters]");
        if (!filterNode) return;
        filterNode.innerHTML = FILTERS.map(([value, label]) => (
            `<label class="filter-chip"><input type="checkbox" value="${value}" data-search-filter> ${label}</label>`
        )).join("");
    }

    function renderSourceScopes(root, defaultScope, allowSourceScope) {
        const sourceNode = root.querySelector("[data-search-source-scopes]");
        if (!sourceNode) return;
        if (!allowSourceScope) {
            sourceNode.innerHTML = "";
            return;
        }
        const selected = defaultScope || "portal";
        const groupName = `search-source-${sourceNode.dataset.sourceGroup || "default"}`;
        sourceNode.innerHTML = SOURCE_SCOPES.map(([value, label]) => (
            `<label class="source-chip"><input type="radio" name="${groupName}" value="${value}" data-search-source ${value === selected ? "checked" : ""}> ${label}</label>`
        )).join("");
    }

    function resultMeta(item) {
        const sourceTitle = item.metadata && item.metadata.source_title ? `from ${item.metadata.source_title}` : "";
        const pieces = [item.source_label, item.kind_label, sourceTitle, item.why].filter(Boolean);
        return pieces.join(" | ");
    }

    async function postJson(path, payload) {
        try {
            await fetch(path, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload),
                keepalive: true,
            });
        } catch (_) {
            // Analytics and feedback should never interrupt the user flow.
        }
    }

    function renderResults(root, data, query) {
        const results = root.querySelector("[data-search-results]");
        const statusNode = root.querySelector("[data-search-status]");
        results.innerHTML = "";
        statusNode.textContent = `${data.total || 0} results`;

        if (!data.results || data.results.length === 0) {
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.innerHTML = `
                <strong>No matching result.</strong>
                <button class="ghost-button small-button" type="button" data-empty-feedback>Report missing result</button>
            `;
            empty.querySelector("[data-empty-feedback]").addEventListener("click", () => {
                const message = prompt("What were you expecting to find?");
                if (message) {
                    postJson("/api/search/feedback", {query, feedback_type: "missing_result", message});
                }
            });
            results.appendChild(empty);
            return;
        }

        for (const item of data.results) {
            const article = document.createElement("article");
            article.className = "result-card";
            const sourceUrl = item.metadata && item.metadata.source_url ? item.metadata.source_url : "";
            const openUrl = item.open_url || item.url;
            const displayUrl = item.display_url || item.url;
            article.innerHTML = `
                <div class="result-card-main">
                    <div class="result-topline">
                        <span class="result-badge">${escapeHtml(item.kind_label || item.category)}</span>
                        <span class="result-score">score ${Number(item.score || 0).toFixed(2)}</span>
                    </div>
                    <a class="result-title" href="${escapeHtml(openUrl)}" target="_blank" rel="noopener" data-result-open>${escapeHtml(item.title)}</a>
                    <div class="meta">${escapeHtml(resultMeta(item))}</div>
                    <p>${highlighted(item.snippet)}</p>
                    <div class="result-url">${escapeHtml(displayUrl)}</div>
                </div>
                <div class="result-actions">
                    <a class="ghost-button small-button" href="${escapeHtml(openUrl)}" target="_blank" rel="noopener" data-result-open>Open</a>
                    ${sourceUrl ? `<a class="ghost-button small-button" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener">Source</a>` : ""}
                    <button class="ghost-button small-button" type="button" data-result-feedback>Feedback</button>
                </div>
            `;
            article.querySelectorAll("[data-result-open]").forEach(link => {
                link.addEventListener("click", () => postJson("/api/search/click", {
                    query,
                    url: item.url,
                    title: item.title,
                    source_type: item.source_type,
                }));
            });
            article.querySelector("[data-result-feedback]").addEventListener("click", () => {
                const message = prompt("What is wrong or missing for this result?");
                if (message) {
                    postJson("/api/search/feedback", {
                        query,
                        url: item.url,
                        title: item.title,
                        feedback_type: "result_feedback",
                        message,
                    });
                }
            });
            results.appendChild(article);
        }
    }

    function mountSearch(root, options) {
        const query = root.querySelector("[data-search-query]");
        const results = root.querySelector("[data-search-results]");
        const statusNode = root.querySelector("[data-search-status]");
        const datalist = root.querySelector("[data-search-suggestions]");
        let timer = null;
        let suggestTimer = null;
        const defaultSourceScope = options && options.sourceScope ? options.sourceScope : "portal";

        renderSourceScopes(root, defaultSourceScope, Boolean(options && options.allowSourceScope));
        renderFilters(root);

        async function runSearch() {
            const q = query.value.trim();
            const filters = selectedFilters(root);
            const sourceScope = selectedSourceScope(root, defaultSourceScope);
            if (q.length < 2) {
                results.innerHTML = "";
                statusNode.textContent = "Type at least 2 characters.";
                return;
            }
            statusNode.textContent = "Searching...";
            const params = new URLSearchParams({q, size: String(options && options.size ? options.size : 30)});
            if (filters.length) params.set("types", filters.join(","));
            params.set("source", sourceScope);
            const response = await fetch(`/api/search?${params.toString()}`);
            const data = await response.json();
            renderResults(root, data, q);
        }

        async function loadSuggestions() {
            if (!datalist) return;
            const q = query.value.trim();
            if (q.length < 1) {
                datalist.innerHTML = "";
                return;
            }
            const sourceScope = selectedSourceScope(root, defaultSourceScope);
            const response = await fetch(`/api/suggest?q=${encodeURIComponent(q)}&size=8&source=${encodeURIComponent(sourceScope)}`);
            const data = await response.json();
            datalist.innerHTML = (data.suggestions || [])
                .map(value => `<option value="${escapeHtml(value)}"></option>`)
                .join("");
        }

        query.addEventListener("input", () => {
            clearTimeout(timer);
            clearTimeout(suggestTimer);
            timer = setTimeout(runSearch, 250);
            suggestTimer = setTimeout(loadSuggestions, 140);
        });
        root.querySelector("[data-search-button]").addEventListener("click", runSearch);
        root.querySelectorAll("[data-search-filter]").forEach(item => item.addEventListener("change", runSearch));
        root.querySelectorAll("[data-search-source]").forEach(item => item.addEventListener("change", runSearch));
        query.addEventListener("keydown", event => {
            if (event.key === "Enter") runSearch();
        });

        return {runSearch};
    }

    window.PortalSearch = {mountSearch, postJson, escapeHtml};
})();
