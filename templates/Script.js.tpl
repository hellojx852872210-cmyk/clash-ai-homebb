// 由 clash-ai-homebb genconfig.py 生成。三件事：
// 1) Clash Verge 有时把全局 Merge 的 prepend-*/append-* 原样写进运行时，mihomo 不认识 → 这里真正合并进去；
// 2) 硬保底：就算 Merge 丢了，也把家宽节点、AI 死链组、日常 provider/组补回来；
// 3) 把「日常出口」插到 Proxies / GLOBAL 等选择组第一位。
var NAMES = __NAMES__;
var PROVIDERS = __PROVIDERS__;
var PROXIES = __PROXIES__;
var GROUPS = __GROUPS__;

function ensureArray(v) { return Array.isArray(v) ? v : []; }
function itemName(p) { return p && typeof p === "object" ? p.name : p; }

function consumeList(config, key, targetKey, prepend) {
  var extra = ensureArray(config[key]);
  delete config[key];
  if (extra.length === 0) return;
  var base = ensureArray(config[targetKey]);
  var seen = {};
  base.forEach(function (x) { var n = itemName(x); if (n) seen[n] = true; });
  var add = [];
  extra.forEach(function (item) {
    var n = itemName(item);
    if (n && seen[n]) return;
    if (n) seen[n] = true;
    add.push(item);
  });
  config[targetKey] = prepend ? add.concat(base) : base.concat(add);
}

function sanitizeRules(rules) {
  // RULE-SET 依赖 rule-providers，Verge 不会把 Merge 的 rule-providers 带过来，留着会让内核拒载
  return ensureArray(rules).filter(function (r) { return String(r).indexOf("RULE-SET,") !== 0; });
}

function ensureProxy(config, proxy) {
  var list = ensureArray(config.proxies);
  for (var i = 0; i < list.length; i++) if (list[i] && list[i].name === proxy.name) return;
  list.unshift(proxy);
  config.proxies = list;
}

function ensureGroup(config, group) {
  // Merge 里已有（prepend 合并进来的）就不动，Merge 优先；没有才补
  var list = ensureArray(config["proxy-groups"]);
  for (var i = 0; i < list.length; i++) if (list[i] && list[i].name === group.name) return;
  list.unshift(group);
  config["proxy-groups"] = list;
}

function injectIntoSelectors(config, name) {
  var targets = { Proxies: 1, GLOBAL: 1, Proxy: 1, "节点选择": 1, PROXY: 1 };
  ensureArray(config["proxy-groups"]).forEach(function (g) {
    if (!g || !Array.isArray(g.proxies) || !targets[g.name]) return;
    g.proxies = g.proxies.filter(function (p) { return p !== name; });
    g.proxies.unshift(name);
  });
}

function main(config, profileName) {
  if (Array.isArray(config["prepend-rules"])) config["prepend-rules"] = sanitizeRules(config["prepend-rules"]);
  if (Array.isArray(config["append-rules"])) config["append-rules"] = sanitizeRules(config["append-rules"]);
  delete config["prepend-rule-providers"];
  delete config["append-rule-providers"];
  consumeList(config, "prepend-proxies", "proxies", true);
  consumeList(config, "append-proxies", "proxies", false);
  consumeList(config, "prepend-proxy-groups", "proxy-groups", true);
  consumeList(config, "append-proxy-groups", "proxy-groups", false);
  consumeList(config, "prepend-rules", "rules", true);
  consumeList(config, "append-rules", "rules", false);

  var pp = config["proxy-providers"];
  if (!pp || typeof pp !== "object" || Array.isArray(pp)) pp = {};
  for (var k in PROVIDERS) if (!pp[k]) pp[k] = PROVIDERS[k];
  config["proxy-providers"] = pp;
  for (var i = 0; i < PROXIES.length; i++) ensureProxy(config, PROXIES[i]);
  for (var g = 0; g < GROUPS.length; g++) ensureGroup(config, GROUPS[g]);
  injectIntoSelectors(config, NAMES.daily);
  return config;
}
