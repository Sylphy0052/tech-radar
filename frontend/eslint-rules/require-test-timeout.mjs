/**
 * テストの持ち時間に `TEST_TIMEOUT_MS` を明示させる ESLint ルール（Issue #47）。
 *
 * `src/test-utils/timeouts.ts` が記録しているとおり、この定数を明示的に付けるか
 * どうかが「持ち時間を意識したかどうか」の印になる。ただし規約として書いてある
 * だけでは、新しいテストを足すときに第3引数を省略しても誰も気づかず、そのテスト
 * だけが既定の5秒に静かに戻る。実際 Issue #40 の作業中に、別ブランチで追加された
 * テストファイルが未適用のまま入っていた。人の記憶ではなく lint で止める。
 *
 * 数値の直書き（`it("...", fn, 5000)`）も違反として扱う。個別に数字を置けるように
 * すると、`timeouts.ts` へ集約した根拠が再び各ファイルへ散らばるため。別の持ち時間
 * がどうしても要るテストは `eslint-disable-next-line` で意図を明示させる。
 */

const TIMEOUT_IDENTIFIER = "TEST_TIMEOUT_MS";
const TEST_FUNCTIONS = new Set(["it", "test"]);
// 本体を取らないため持ち時間の概念がない修飾子。
const BODYLESS_MODIFIER = "todo";
// `it.each(...)` は「テストを作る呼び出し」で、実際のテストはその戻り値の呼び出し。
const EACH_MODIFIER = "each";

/**
 * 呼び出し対象の式を分解し、根になる識別子・経由した修飾子・呼び出し済みかを返す。
 *
 * `it.skip.each([...])(...)` のような連なりも、`it` まで遡って一様に扱えるようにする。
 * 解釈できない形（計算プロパティなど）は null を返し、対象外として扱う。
 */
function analyzeCallee(node) {
  if (node.type === "Identifier") {
    return { root: node.name, modifiers: [], applied: false };
  }
  if (node.type === "MemberExpression" && !node.computed && node.property.type === "Identifier") {
    const inner = analyzeCallee(node.object);
    return inner === null
      ? null
      : { ...inner, modifiers: [...inner.modifiers, node.property.name] };
  }
  if (node.type === "CallExpression") {
    const inner = analyzeCallee(node.callee);
    return inner === null ? null : { ...inner, applied: true };
  }
  // `` it.each`table` `` のタグ付きテンプレート記法。現状のコードでは使っていないが、
  // 検査を素通りさせないため呼び出し済みとして扱う。
  if (node.type === "TaggedTemplateExpression") {
    const inner = analyzeCallee(node.tag);
    return inner === null ? null : { ...inner, applied: true };
  }
  return null;
}

/** 持ち時間を渡すべきテスト定義の呼び出しかを判定する。 */
function isTimedTestCall(callee) {
  const info = analyzeCallee(callee);
  if (info === null || !TEST_FUNCTIONS.has(info.root)) {
    return false;
  }
  if (info.modifiers.includes(BODYLESS_MODIFIER)) {
    return false;
  }
  // `each` を使う形では外側の呼び出しだけが、使わない形では `it(...)` 自体が実テスト。
  return info.modifiers.includes(EACH_MODIFIER) ? info.applied : !info.applied;
}

/** @type {import("eslint").Rule.RuleModule} */
const requireTestTimeout = {
  meta: {
    type: "problem",
    docs: {
      description: `テストの持ち時間に ${TIMEOUT_IDENTIFIER} を明示させる`,
    },
    schema: [],
    messages: {
      missingTimeout: `テストの持ち時間が指定されていません。第3引数へ ${TIMEOUT_IDENTIFIER} を渡してください（付けないと vitest 既定の5000msになり、高負荷時にだけ落ちます）。`,
      useSharedTimeout: `テストの持ち時間には ${TIMEOUT_IDENTIFIER} を使ってください。別の値がどうしても要る場合は eslint-disable-next-line で理由を明示してください。`,
    },
  },
  create(context) {
    return {
      CallExpression(node) {
        if (!isTimedTestCall(node.callee)) {
          return;
        }

        const timeout = node.arguments[2];
        if (timeout === undefined) {
          context.report({ node, messageId: "missingTimeout" });
          return;
        }
        if (timeout.type !== "Identifier" || timeout.name !== TIMEOUT_IDENTIFIER) {
          context.report({ node: timeout, messageId: "useSharedTimeout" });
        }
      },
    };
  },
};

export default requireTestTimeout;
