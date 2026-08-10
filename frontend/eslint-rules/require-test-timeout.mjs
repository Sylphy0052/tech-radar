/**
 * テストの持ち時間に `TEST_TIMEOUT_MS` を明示させる ESLint ルール（Issue #47）。
 *
 * `src/test-utils/timeouts.ts` が記録しているとおり、この定数を明示的に付けるか
 * どうかが「持ち時間を意識したかどうか」の印になる。ただし規約として書いてある
 * だけでは、新しいテストを足すときに持ち時間を省略しても誰も気づかず、そのテスト
 * だけが既定の5秒に静かに戻る。実際 Issue #40 の作業中に、別ブランチで追加された
 * テストファイルが未適用のまま入っていた。人の記憶ではなく lint で止める。
 *
 * 数値の直書き（`it("...", fn, 5000)`）も違反として扱う。個別に数字を置けるように
 * すると、`timeouts.ts` へ集約した根拠が再び各ファイルへ散らばるため。別の持ち時間
 * がどうしても要るテストは `eslint-disable-next-line` で意図を明示させる。
 *
 * 既知の限界（いずれも意図して検査対象から外している）:
 *
 * - 第3引数が `TEST_TIMEOUT_MS` という名前かどうかしか見ない。同名のローカル変数を
 *   宣言して渡せば通る。束縛まで辿るとルールが重くなるうえ、そこまでして規約を
 *   迂回する書き方は lint ではなくレビューで気づくべきものと判断した
 * - `it` を別名で import した場合（`import { it as vIt }`）や計算プロパティ経由の
 *   呼び出し（`it["skip"](...)`）は検出しない
 * - 引数にスプレッドが混ざる呼び出しは、静的に第何引数かを決められないため対象外
 */

const TIMEOUT_IDENTIFIER = "TEST_TIMEOUT_MS";
const TIMEOUT_OPTION_KEY = "timeout";
const TEST_FUNCTIONS = new Set(["it", "test"]);
// 本体を取らないため持ち時間の概念がない修飾子。
const BODYLESS_MODIFIER = "todo";
// `it.each(...)` / `it.for(...)` は「テストを作る呼び出し」で、実際のテストは
// その戻り値の呼び出しのほう。
const TABLE_MODIFIERS = new Set(["each", "for"]);

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
  // 表形式では外側の呼び出しだけが、そうでなければ `it(...)` 自体が実テスト。
  return info.modifiers.some((modifier) => TABLE_MODIFIERS.has(modifier))
    ? info.applied
    : !info.applied;
}

function isSharedTimeout(node) {
  return node.type === "Identifier" && node.name === TIMEOUT_IDENTIFIER;
}

/**
 * オプションオブジェクトから `timeout` の値を取り出す。
 *
 * 見つからなければ undefined を返す。スプレッドが混ざっていて静的に決められない
 * 場合は null を返し、呼び出し側で対象外にする。
 */
function findTimeoutOption(objectExpression) {
  let value;
  for (const property of objectExpression.properties) {
    if (property.type !== "Property") {
      return null;
    }
    if (property.computed) {
      continue;
    }
    const key = property.key;
    const name = key.type === "Identifier" ? key.name : key.type === "Literal" ? key.value : null;
    if (name === TIMEOUT_OPTION_KEY) {
      value = property.value;
    }
  }
  return value;
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
      missingTimeout: `テストの持ち時間が指定されていません。${TIMEOUT_IDENTIFIER} を渡してください（付けないと vitest 既定の5000msになり、高負荷時にだけ落ちます）。`,
      useSharedTimeout: `テストの持ち時間には ${TIMEOUT_IDENTIFIER} を使ってください。別の値がどうしても要る場合は eslint-disable-next-line で理由を明示してください。`,
    },
  },
  create(context) {
    return {
      CallExpression(node) {
        if (!isTimedTestCall(node.callee)) {
          return;
        }
        if (node.arguments.some((argument) => argument.type === "SpreadElement")) {
          return;
        }

        // vitest は `(name, fn, timeout)` と `(name, options, fn)` の両方を取る。
        // `it.for` は後者しか取らないため、オプション形式を先に見る。
        const options = node.arguments[1];
        if (options !== undefined && options.type === "ObjectExpression") {
          const timeout = findTimeoutOption(options);
          if (timeout === null) {
            return;
          }
          if (timeout === undefined) {
            context.report({ node: options, messageId: "missingTimeout" });
            return;
          }
          if (!isSharedTimeout(timeout)) {
            context.report({ node: timeout, messageId: "useSharedTimeout" });
          }
          return;
        }

        const timeout = node.arguments[2];
        if (timeout === undefined) {
          context.report({ node, messageId: "missingTimeout" });
          return;
        }
        if (!isSharedTimeout(timeout)) {
          context.report({ node: timeout, messageId: "useSharedTimeout" });
        }
      },
    };
  },
};

export default requireTestTimeout;
