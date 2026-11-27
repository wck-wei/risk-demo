"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 });

const modules = {
  risk: ["风控入口", "接收并检查业务申请。处理结束后，负责把决定交还业务方。"],
  engine: ["决策引擎", "安排数据查询、规则检查和模型评估，把各方结果合成一个业务决定。"],
  feature: ["特征网关", "按已发布的数据口径查询、计算并汇总风险特征，再返回给规则引擎或模型在线服务。"],
  model: ["模型在线服务", "通过 Magic Cube 接收实体和业务上下文，自行到特征网关取所需数据。完成评估后将建议交给引擎。"],
  rcms: ["RCMS", "配置并发布规则流，定义检查的先后顺序，以及命中时走拦截、验证还是复核。"],
  metadata: ["元信息平台", "发布数据源结构和业务口径。特征网关据此分发请求，区分本地取值、快照读取和实时窗口计算。"],
  registry: ["Magic Cube", "模型版本先注册并校验，部署后由在线接口提供服务。切换版本后，可重新部署仍保留的旧版本。"],
};

const eventNames = {
  "risk.received": "接收申请",
  "rcms.route": "读取已发布规则流",
  "model.gateway": "调用 Magic Cube 在线接口",
  "engine.strategy": "选择检查流程",
  "feature.batch_get": "批量查询数据",
  "engine.rules": "核对业务规则",
  "model.predict": "执行模型评估",
  "feature.retry": "补取缺失数据",
  "engine.decision": "汇总处理结果",
  "risk.response": "把结果返回业务方",
};

const featureNames = {
  cust_risk_prob: "客户风险指标",
  overdue_30d_cnt: "近期逾期次数",
  multi_loan_cnt: "多头借贷数量",
  income_level: "收入等级",
  device_risk_score: "设备风险指标",
  age: "年龄",
  order_amt_7d: "近 7 天订单金额",
  merchant_risk_level: "商户风险等级",
  repay_capacity_score: "还款能力指标",
  available_credit: "可用额度",
  utilization_rate: "额度使用率",
  ltv_score: "客户价值指标",
  competitor_rate: "竞品年利率",
  term_months: "还款期限",
  watchlist_for_review: "名单复核信号",
  device_verification_signal: "设备验证信号",
};

const reasons = {
  AML_MANUAL_REVIEW: "合成名单出现待核验信号，转交人工复核。",
  FRAUD_DEVICE_VERIFY: "演示设备风险信号触发二次验证。",
  MODEL_UNAVAILABLE: "在线接口没有可用的模型版本，进入备用流程。",
  CREDIT_AGE_OUT_OF_RANGE: "年龄超出演示授信规则范围。",
  CREDIT_OVERDUE: "近 30 天有逾期记录。",
  CREDIT_MULTI_LOAN: "多头借贷数量超过演示上限。",
  CREDIT_DEVICE_RISK: "设备风险达到拦截条件。",
  CREDIT_SCORE_BELOW_THRESHOLD: "评分低于授信门槛，需要进一步复核。",
  ORDER_INSUFFICIENT_CREDIT: "订单金额超过可用额度。",
  ORDER_MERCHANT_RISK: "商户风险等级超过演示上限。",
  ORDER_SEVEN_DAY_LIMIT: "本单与近 7 天订单合计超过规则上限。",
  ORDER_MODEL_CHALLENGE: "欺诈风险达到验证门槛。",
  FEATURE_TIMEOUT_FALLBACK: "查询所需数据超时，按备用流程处理。",
  FEATURE_MISSING: "部分评估数据缺失、过期或不可用。",
  FEATURE_VERSION_MISMATCH: "数据版本与当前评估流程不匹配。",
  MODEL_MISSING_FEATURE: "模型需要的数据一度缺失，Magic Cube 发起了补取。",
  MODEL_TIMEOUT_RULE_ONLY: "规则检查已完成，模型超时，需要二次验证。",
  MODEL_TIMEOUT_REVIEW: "模型超时，交由人工复核。",
  LOW_RISK_PREMIUM: "按低风险档计算演示报价。",
  HIGH_RISK_PREMIUM: "按高风险档计算演示报价。",
  COMPETITOR_RATE_MATCHED: "竞品报价在允许匹配的区间内，已采用更低报价。",
};

const stageNames = { AML: "反洗钱筛查", BUSINESS: "业务规则", FRAUD: "反欺诈检查" };
const routeNames = { LOCAL: "本地取值", TABLE: "快照读取", OUTER_API: "交给计算服务" };

function eventLabel(event) {
  if (event.event === "engine.rules") return stageNames[event.stage] || eventNames[event.event];
  if (event.event === "feature.batch_get" && event.consumer === "magic_cube") return "模型查询所需数据";
  return eventNames[event.event];
}

const verdicts = {
  PASS: ["授信通过", "符合授信规则和评分要求，可以按申请金额与期限提供授信。", "success"],
  ALLOW: ["下单放行", "可用额度和风险检查均满足要求，可以继续下单。", "success"],
  PRICE_OK: ["定价完成", "系统按模拟风险溢价和允许的利率区间给出报价。", "success"],
  REJECT: ["未通过", "触发了必须拦截的业务规则，申请在当前环节结束。", "reject"],
  CHALLENGE: ["需要二次验证", "完成额外验证后，再由业务方继续处理订单。", "review"],
  REVIEW: ["转人工复核", "系统暂缓自动决策，交由人工进一步核对。", "review"],
};

function element(tag, content) {
  const item = document.createElement(tag);
  item.textContent = content;
  return item;
}

function setList(selector, values) {
  $(selector).replaceChildren(...values.map((value) => element("li", value)));
}

function setFacts(selector, rows) {
  $(selector).replaceChildren(...rows.map(([label, value]) => {
    const row = document.createElement("div");
    row.append(element("dt", label), element("dd", value));
    return row;
  }));
}

function friendlyFeature(name, value) {
  if (value === undefined) return featureNames[name] || name;
  const format = ["cust_risk_prob", "competitor_rate", "utilization_rate"].includes(name) ? percent : number;
  return `${featureNames[name] || name} · ${format.format(value)}`;
}

const sceneButtons = $$("[data-scene]");
const moduleButtons = $$("[data-module]");
const caseSelect = $("#case");
const playButton = $("#play");
const previousButton = $("#previous");
const nextButton = $("#next");
const resetButton = $("#reset");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

let scene = "credit";
let demo;
let events = [];
let timelineButtons = [];
let step = -1;
let furthest = -1;
let selectedModule = "risk";
let timer = null;

const moduleFor = (event) => event?.event.split(".")[0];
const current = () => events[step];
const finished = () => step === events.length - 1;

function explain(event) {
  const response = demo.response;
  const facts = [];
  switch (event.event) {
    case "risk.received":
      return ["业务提交申请", "入口检查资料是否齐全，并把申请交给决策引擎。", facts];
    case "rcms.route":
      return ["选择已发布规则流", "RCMS 提供本场景的检查顺序。命中需要处理的规则时，流程在当前步骤分流。",
        [`发布版本 · ${event.flow_version}`, `执行顺序 · ${event.stages.map((stage) => stageNames[stage]).join(" → ")}`]];
    case "engine.strategy":
      return ["安排本次检查", "引擎根据业务类型确定需要查询的数据和执行的检查。", facts];
    case "feature.batch_get":
    case "feature.retry": {
      const retry = event.event === "feature.retry";
      if (event.status === "TIMEOUT") {
        return ["查询数据超时", "在允许的处理时间内未取到数据，引擎会按备用流程处理。", facts];
      }
      const values = response.component_results.features.feature_values;
      const routeLabels = [...new Set(Object.values(event.routes).map((route) => routeNames[route]))];
      if (routeLabels.length) facts.push(`取数方式 · ${routeLabels.join("、")}`);
      event.names.filter((name) => !event.missing_features.includes(name)).slice(0, 4)
        .forEach((name) => facts.push(friendlyFeature(name, values[name])));
      if (event.missing_features.length) {
        facts.push(`尚未取得 · ${event.missing_features.map((name) => friendlyFeature(name)).join("、")}`);
      }
      if (event.defaulted.length) {
        facts.push(`允许使用默认值 · ${event.defaulted.map((name) => friendlyFeature(name)).join("、")}`);
      }
      const title = retry ? (event.missing_features.length ? "补取后仍缺少数据" : "补取到所需数据") : "批量查询风险数据";
      const message = retry ? "特征网关把回补结果交给 Magic Cube，供模型继续评估。"
        : event.consumer === "magic_cube" ? "Magic Cube 根据模型需要发起批量查询，特征网关返回模型输入。"
          : "引擎查询规则需要的数据。特征网关按元信息分发请求，并汇总各数据源结果。";
      return [title, message, facts];
    }
    case "engine.rules":
      if (event.hit_rules.length) {
        return [`${stageNames[event.stage]}命中`, "引擎按照规则处理这笔申请，流程跳过模型评估。",
          event.hit_rules.map((rule) => reasons[rule] || "触发预设业务规则。")];
      }
      if (event.missing_features.length) {
        return ["规则无法完成", "引擎缺少执行规则所需的数据，改走备用流程。", facts];
      }
      if (event.stage === "AML") return ["名单筛查完成", "本次未命中需要人工复核的合成名单信号。", facts];
      if (event.stage === "FRAUD") return ["反欺诈检查完成", "本次未触发该阶段的设备风险规则。", facts];
      return ["业务规则检查完成",
        { credit: "检查年龄、逾期和多头借贷，本次未触发拦截。",
          order: "检查可用额度、商户风险和近 7 天的累计订单金额，本次未触发拦截。",
          pricing: "当前定价流程没有前置硬规则拦截，引擎将进入报价评估。" }[scene], facts];
    case "model.gateway":
      return ["访问 Magic Cube 在线接口",
        event.model_version ? "在线接口选定当前部署版本，再向特征网关查询模型所需数据。"
          : "当前没有部署中的模型，接口返回不可用。",
        event.model_version ? [`在线版本 · ${event.model_version}`] : facts];
    case "model.predict": {
      if (event.status === "MISSING_FEATURES") {
        const missing = event.missing_features.map((name) => friendlyFeature(name)).join("、");
        const alreadyRetried = events.slice(0, event.sequence - 1).some((item) => item.event === "feature.retry");
        const message = alreadyRetried ? "已达到补取次数上限，引擎会转入备用流程。"
          : "评估发现数据不全，Magic Cube 会向特征网关补取一次。";
        return ["模型发现数据缺项", message, [`缺少 · ${missing}`]];
      }
      if (event.status === "TIMEOUT") {
        return ["模型未按时返回", "引擎会依据本场景的备用流程，决定二次验证或人工复核。", facts];
      }
      if (event.status !== "OK") {
        return ["模型结果不可用", "当前评估无法使用，引擎将按备用流程处理。", facts];
      }
      const model = response.component_results.model;
      if (scene === "credit") {
        facts.push(`授信评分 · ${number.format(model.score * 100)} / 100`,
          `通过门槛 · ${number.format(model.threshold * 100)} / 100`);
      } else if (scene === "order") {
        facts.push(`模拟欺诈概率 · ${percent.format(model.probability)}`,
          `验证门槛 · ${percent.format(model.threshold)}`);
      } else {
        facts.push(`基础年利率 · ${percent.format(demo.request.base_rate)}`,
          `模拟风险溢价 · ${percent.format(model.risk_premium)}`,
          `模型建议年利率 · ${percent.format(model.suggested_rate)}`);
      }
      return ["模型返回评估建议", "模型使用 Magic Cube 查询到的数据完成评估，并把建议交回引擎。", facts];
    }
    case "engine.decision":
      return ["引擎汇总决定", "引擎结合适用规则、模型或备用流程，形成供入口返回的业务结果。", facts];
    case "risk.response":
      return ["处理完成", "入口把结果交给业务方。业务结果区显示了处理决定和相关原因。", facts];
    default:
      return ["处理请求", "", facts];
  }
}

function renderDetail() {
  const relevant = events.slice(0, step + 1).findLast((event) => moduleFor(event) === selectedModule);
  const [moduleName, role] = modules[selectedModule];
  const platformFacts = {
    rcms: [`当前发布版本 · ${demo.response.component_results.engine.flow_version}`],
    metadata: [`当前发布版本 · ${demo.metadata_release}`, "数据源结构说明可用的原料，特征定义说明如何得到指标。"],
    registry: ["注册 → 校验 → 部署 → 切换或下线"],
  };
  const [title, explanation, facts] = relevant ? explain(relevant)
    : ["模块职责", role, platformFacts[selectedModule] || []];
  const routes = {
    "risk.received": "风控入口 → 决策引擎",
    "rcms.route": "RCMS → 风控入口",
    "feature.batch_get": relevant?.consumer === "magic_cube" ? "Magic Cube ↔ 特征网关" : "决策引擎 ↔ 特征网关",
    "feature.retry": "Magic Cube ↔ 特征网关",
    "model.gateway": "决策引擎 → Magic Cube",
    "model.predict": "Magic Cube → 决策引擎",
    "risk.response": "决策引擎 → 风控入口 → 业务方",
  };
  $("#detail-module").textContent = routes[relevant?.event] || moduleName;
  $("#detail-title").textContent = title;
  $("#detail-text").textContent = explanation;
  setList("#detail-facts", [...facts, ...(demo.platform_activity[selectedModule] || [])]);
}

function renderResult() {
  const result = $("#result");
  const data = demo.response;
  const [title, text, tone] = finished() ? verdicts[data.decision]
    : [step < 0 ? "尚未开始" : "正在处理", "流程结束后，这里会给出业务决定和原因。", ""];
  result.dataset.tone = tone;
  $("#result-title").textContent = title;
  $("#result-text").textContent = text;
  const rows = [];
  if (finished()) {
    if (data.approved_amount !== null) {
      rows.push([scene === "credit" ? "授信额度" : "放行金额", number.format(data.approved_amount)]);
    }
    if (data.approved_term !== null) rows.push(["还款期限", `${data.approved_term} 期`]);
    if (data.final_rate !== null) rows.push(["最终年利率", percent.format(data.final_rate)]);
    if (data.apr !== null) rows.push(["演示 APR", percent.format(data.apr)]);
    if (data.risk_level === "LOW" || data.risk_level === "HIGH") {
      rows.push(["模拟风险等级", data.risk_level === "LOW" ? "低" : "高"]);
    }
    if (data.need_verify !== null) rows.push(["二次验证", data.need_verify ? "需要" : "无需"]);
  }
  setFacts("#result-facts", rows);
  setList("#result-reasons", finished() ? data.reason_codes.map((reason) => reasons[reason] || "按演示规则处理。") : []);
}

function renderFlow() {
  const active = moduleFor(current());
  const visited = new Set(events.slice(0, step + 1).map(moduleFor));
  const route = active === "feature" || active === "model" ? active : "";
  moduleButtons.forEach((button) => {
    const name = button.dataset.module;
    button.classList.toggle("current", active === name);
    button.classList.toggle("visited", visited.has(name));
    button.setAttribute("aria-pressed", String(name === selectedModule));
  });
  $("#diagram").dataset.route = route;
  $("#diagram").dataset.consumer = route === "feature" ? current().consumer : "";
  $('[data-module="metadata"]').classList.toggle("current", active === "feature");
  $('[data-module="registry"]').classList.toggle("current", current()?.event === "model.gateway");
  $(".intake").classList.toggle("active", active === "risk" || current()?.event === "engine.strategy");
  $("#flow-count").textContent = `${step + 1} / ${events.length} 步`;
  timelineButtons.forEach((button, index) => {
    button.disabled = index > furthest;
    if (index === step) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  });
  previousButton.disabled = step < 0;
  nextButton.disabled = finished();
  resetButton.disabled = furthest < 0;
  $("#play-text").textContent = timer !== null ? "暂停播放" : finished() ? "重新播放"
    : step >= 0 ? "继续播放" : "播放流程";
  $("#play-icon").textContent = timer === null ? "▶" : "Ⅱ";
  renderDetail();
  renderResult();
}

function stop() {
  if (timer !== null) window.clearInterval(timer);
  timer = null;
}

function goTo(index) {
  step = Math.max(-1, Math.min(index, events.length - 1));
  furthest = Math.max(furthest, step);
  selectedModule = moduleFor(current()) || "risk";
  if (finished()) stop();
  renderFlow();
}

function clearProgress() {
  stop();
  furthest = -1;
  goTo(-1);
}

function loadCase() {
  stop();
  demo = window.demoCases[scene][caseSelect.value];
  events = demo.response.component_results.component_trace;
  $("#case-story").textContent = demo.story;
  const request = demo.request;
  const rows = [["申请金额", number.format(request.amount)], ["还款期限", `${request.term_months} 期`]];
  if (scene === "order") rows[0][0] = "订单金额";
  if (request.base_rate !== undefined) rows.push(["基础年利率", percent.format(request.base_rate)]);
  if (request.competitor_rate !== undefined) rows.push(["竞品年利率", percent.format(request.competitor_rate)]);
  setFacts("#case-facts", rows);
  const steps = events.map((event, index) => {
    const item = document.createElement("li");
    const button = element("button", eventLabel(event));
    button.type = "button";
    button.addEventListener("click", () => {
      stop();
      goTo(index);
    });
    item.append(button);
    return item;
  });
  $("#timeline").replaceChildren(...steps);
  timelineButtons = steps.map((item) => item.firstElementChild);
  clearProgress();
}

function selectScene(name) {
  scene = name;
  sceneButtons.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.scene === name)));
  caseSelect.replaceChildren(...Object.entries(window.demoCases[name]).map(([key, entry]) => {
    const option = element("option", entry.title);
    option.value = key;
    return option;
  }));
  loadCase();
}

if (window.demoCases) {
  sceneButtons.forEach((button) => button.addEventListener("click", () => {
    if (scene !== button.dataset.scene) selectScene(button.dataset.scene);
  }));
  moduleButtons.forEach((button) => button.addEventListener("click", () => {
    stop();
    selectedModule = button.dataset.module;
    renderFlow();
  }));
  caseSelect.addEventListener("change", loadCase);
  playButton.addEventListener("click", () => {
    if (timer !== null) {
      stop();
      renderFlow();
      return;
    }
    if (finished()) clearProgress();
    timer = window.setInterval(() => goTo(step + 1), reducedMotion.matches ? 1500 : 1250);
    goTo(step + 1);
  });
  previousButton.addEventListener("click", () => { stop(); goTo(step - 1); });
  nextButton.addEventListener("click", () => { stop(); goTo(step + 1); });
  resetButton.addEventListener("click", clearProgress);
  selectScene(scene);
} else {
  document.querySelectorAll(".setup button, .setup select, .module").forEach((control) => { control.disabled = true; });
  $("#detail-title").textContent = "演示数据未加载";
  $("#detail-text").textContent = "请解压完整项目后，重新打开 demo/index.html。";
}
