// ae_inspect.jsx — trusted After Effects inspection script (ExtendScript, ES3).
// Run by the guest runner:  AfterFX.com -noui -r ae_inspect.jsx   with PB_CTL=<ctl dir> in the env.
// Reads <PB_CTL>/ae_inspect_request.json = {"project": ..., "report": ..., "workspace": ...}
// (ASCII JSON written by ae_adapter.py; parsed by the strict flat parser below, never eval'd),
// opens the project with dialogs suppressed, writes the report JSON, closes without saving, quits.
(function () {
    var CAP = 200, budget = 20000;

    function enc(s) {
        s = String(s);
        var out = '"';
        for (var i = 0; i < s.length; i++) {
            var ch = s.charAt(i), c = s.charCodeAt(i);
            if (ch === '"' || ch === '\\') out += '\\' + ch;
            else if (c < 32 || c > 126) out += '\\u' + ('0000' + c.toString(16)).slice(-4);
            else out += ch;
        }
        return out + '"';
    }

    function toJSON(v) {                       // ExtendScript has no JSON object
        if (v === null || v === undefined) return 'null';
        var t = typeof v;
        if (t === 'number') return isFinite(v) ? String(v) : 'null';
        if (t === 'boolean') return v ? 'true' : 'false';
        if (t === 'string') return enc(v);
        if (v instanceof Array) {
            var a = [];
            for (var i = 0; i < v.length; i++) a.push(toJSON(v[i]));
            return '[' + a.join(',') + ']';
        }
        var o = [];
        for (var k in v) if (v.hasOwnProperty(k)) o.push(enc(k) + ':' + toJSON(v[k]));
        return '{' + o.join(',') + '}';
    }

    function parseRequest(text) {              // strict {"k":"v",...} with JSON string escapes only
        var i = 0, out = {};
        function fail() { throw new Error("bad request file"); }
        function ws() { while (i < text.length && " \t\r\n".indexOf(text.charAt(i)) >= 0) i++; }
        function str() {
            if (text.charAt(i++) !== '"') fail();
            var s = "";
            while (i < text.length) {
                var ch = text.charAt(i++);
                if (ch === '"') return s;
                if (ch !== '\\') { s += ch; continue; }
                var e = text.charAt(i++);
                if (e === 'u') { s += String.fromCharCode(parseInt(text.substr(i, 4), 16)); i += 4; }
                else if (e === 'n') s += '\n';
                else if (e === 't') s += '\t';
                else if (e === 'r') s += '\r';
                else if (e === '"' || e === '\\' || e === '/') s += e;
                else fail();
            }
            fail();
        }
        ws();
        if (text.charAt(i++) !== '{') fail();
        ws();
        while (text.charAt(i) !== '}') {
            var k = str();
            ws();
            if (text.charAt(i++) !== ':') fail();
            ws();
            out[k] = str();
            ws();
            if (text.charAt(i) === ',') { i++; ws(); }
            if (i >= text.length) fail();
        }
        return out;
    }

    function readFile(path) {
        var f = new File(path);
        f.encoding = "UTF-8";
        if (!f.open("r")) throw new Error("cannot read " + path);
        var t = f.read();
        f.close();
        return t;
    }

    function writeFile(path, text) {
        var f = new File(path);
        f.encoding = "UTF-8";
        f.lineFeed = "Unix";
        if (!f.open("w")) throw new Error("cannot write report");
        f.write(text);
        f.close();
    }

    function push(list, s) { if (list.length < CAP) list.push(String(s).slice(0, 300)); }

    function exprErrors(group, label, rep) {
        for (var i = 1; i <= group.numProperties && budget > 0; i++) {
            var p = group.property(i);
            budget--;
            if (p.propertyType === PropertyType.PROPERTY) {
                if (p.canSetExpression && p.expressionEnabled && p.expressionError)
                    push(rep.warnings, "expression error on " + label + "/" + p.name + ": " + p.expressionError);
            } else {
                exprErrors(p, label, rep);
            }
        }
    }

    function inspect(proj, workspace, rep) {
        var i, j, k, installed = {}, seenFx = {};
        var hdr = /PQ|HLG|2100/.test(String(proj.workingSpace || ""));
        try {
            for (i = 0; i < app.effects.length; i++) installed[app.effects[i].matchName] = true;
        } catch (e0) { installed = null; push(rep.limitations, "installed-effect inventory unavailable"); }
        var wsLower = String(workspace).replace(/\\/g, "/").toLowerCase();
        for (i = 1; i <= proj.items.length; i++) {
            var it = proj.items[i];
            if (it instanceof FootageItem && it.footageMissing) {
                var p = it.file ? it.file.fsName : it.name, pf = String(p).replace(/\\/g, "/");
                if (pf.toLowerCase().indexOf(wsLower + "/") === 0) p = pf.substr(wsLower.length + 1);
                push(rep.missing_media, p);
            }
            if (!(it instanceof CompItem)) continue;
            var c = {id: String(it.id), name: String(it.name), width: it.width, height: it.height,
                     pixel_aspect: it.pixelAspect, fps: it.frameRate,
                     duration_frames: Math.round(it.duration * it.frameRate), has_audio: false,
                     alpha_required: false, bit_depth: proj.bitsPerChannel, hdr: hdr};
            for (j = 1; j <= it.numLayers; j++) {
                var L = it.layer(j);
                try { if (L.hasAudio && L.audioEnabled) c.has_audio = true; } catch (e1) {}
                try {
                    var fx = installed ? L.property("ADBE Effect Parade") : null;
                    if (fx) for (k = 1; k <= fx.numProperties; k++) {
                        var mn = fx.property(k).matchName;
                        if (!installed[mn] && !seenFx[mn]) {
                            seenFx[mn] = true;
                            push(rep.missing_effects, (fx.property(k).name || mn) + " (" + it.name + ")");
                        }
                    }
                } catch (e2) {}
                try { exprErrors(L, it.name + "/" + L.name, rep); } catch (e3) {}
            }
            if (rep.comps.length < 500) rep.comps.push(c);
        }
        try {
            var uf = proj.usedFonts;
            if (uf === undefined || uf === null) throw 0;
            for (i = 0; i < uf.length; i++) {
                var f = uf[i].font;
                if (f && f.isSubstitute) push(rep.missing_fonts, f.postScriptName || f.familyName || "unknown font");
            }
        } catch (e4) { push(rep.limitations, "font check requires After Effects 24.0 or newer"); }
    }

    var rep = {comps: [], missing_media: [], missing_fonts: [], missing_effects: [], warnings: [],
               errors: [], limitations: [], ae_version: String(app.version)};
    var req = null;
    try {
        var ctl = $.getenv("PB_CTL");
        if (!ctl) throw new Error("PB_CTL not set");
        req = parseRequest(readFile(ctl + "/ae_inspect_request.json"));
        if (!req.project || !req.report) throw new Error("request incomplete");
        app.beginSuppressDialogs();
        var proj = app.open(new File(req.project));
        if (!proj) throw new Error("project did not open");
        inspect(proj, req.workspace || "", rep);
    } catch (e) {
        push(rep.errors, "inspection failed: " + String(e && e.message ? e.message : e));
    }
    try { if (req && req.report) writeFile(req.report, toJSON(rep)); } catch (e5) {}
    try { if (app.project) app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES); } catch (e6) {}
    try { app.endSuppressDialogs(false); } catch (e7) {}
    app.quit();
})();
