:- encoding(utf8).
% =====================================================================
% rpg 包：市场。价格随供需变，商店的货得自己做出来。
%
% 每个游戏小时（tick 阶段 market）：
%   商店缺货      商人（Person.trader）的 sells 列出卖哪几类、每类备几件（shop_stock，默认 1）；
%                 缺的记成需求，也成了他自己想做的事（want：Collect item kind:<类别>，规划器去采集、制造或收购）
%   上架          商人手上属于他卖的那几类东西 for_sale = true（做完任何事之后也马上上架）
%   价格          price = value × 稀缺 × 需求，夹在 0.5–3 倍之间，至少 1
%                 稀缺 = (基准数量 + 2) / (现有数量 + 2)    基准 = 第一次看到这类东西时世上有几件
%                 需求 = 1 + 0.15 × 最近的需求（有人买、商店缺货都会加；每小时衰减 30%）
% 每类东西一个 (:MarketGood {name: 类别, base, supply, demand, price, prev, factor}) 节点。
% 钱从不凭空产生：只在买卖时从一个人转到另一个人（buy / sell 动作）。
% =====================================================================

:- discontiguous after/2, context/2, context/3, panel/3, want/2.

reads(label('MarketGood')).
reads(label('Item', [value, item_kind, price, for_sale])).

const(min_factor, 0.5).
const(max_factor, 3.0).
const(demand_step, 0.15).
const(demand_decay, 0.7).
const(shortage_demand, 1.0).
const(shop_priority, 0.78).

market_period(P) :- day_length(L), P is L / 24.
tick_period(P) :- market_period(P).
tick_phase(300, market).

% ---------------------------------------------------------------------
% 词汇
% ---------------------------------------------------------------------

kind_of(I, K) :- prop(I, item_kind, K0), atom(K0), K0 \== '', downcase_atom(K0, K).

trader(P) :- prop(P, trader, true).
sells(P, Ks) :- trader(P), ( prop(P, sells, Raw) -> kind_list(Raw, Ks) ; Ks = [] ), Ks \== [].
kind_list(L, Ks) :- is_list(L), !, findall(K, (member(X, L), atom_string(A, X), A \== '', downcase_atom(A, K)), Ks).
kind_list(A, Ks) :- atom(A), catch(atom_json_term(A, L, []), _, fail), is_list(L), !, kind_list(L, Ks).
kind_list(A, Ks) :- atom(A), !, split_string(A, ",", " ", Parts), kind_list(Parts, Ks).
kind_list(_, []).
shop_stock(P, N) :- ( prop(P, shop_stock, N0), number(N0) -> N = N0 ; N = 1 ).

held(P, K, N) :- aggregate_all(count, (rel('HAS_ITEM', P, I), kind_of(I, K)), N).
shortage(P, K) :- sells(P, Ks), shop_stock(P, S), member(K, Ks), held(P, K, N), N < S.
unlisted(P, I) :- sells(P, Ks), rel('HAS_ITEM', P, I), kind_of(I, K), memberchk(K, Ks), \+ prop(I, for_sale, true).

good(K, Key, V) :- node('MarketGood', K), prop(K, Key, V).

factor(Base, Supply, Demand, F) :-
    const(min_factor, Lo), const(max_factor, Hi), const(demand_step, Step),
    F is max(Lo, min(Hi, (Base + 2.0) / (Supply + 2.0) * (1.0 + Step * Demand))).

% ---------------------------------------------------------------------
% 每个游戏小时
% ---------------------------------------------------------------------

market_due :- market_period(P), every(P).

tick(market, E) :- market_due, ( unlisted(_, I), listed(I, E) ; market_effects(Es), member(E, Es) ).

listed(I, _{op: set_prop, entity: I, label: 'Item', prop: for_sale, value: true}).

market_effects(Es) :-
    findall(K, ( node('Item', I), kind_of(I, K), prop(I, value, V), number(V), V > 0 ), Ks0), sort(Ks0, Present),
    findall(K, node('MarketGood', K), Known),
    findall(E, ( member(K, Present), price_effect(K, E) ), Es1),
    findall(_{op: set_prop, entity: K, label: 'MarketGood', prop: supply, value: 0},
            ( member(K, Known), \+ memberchk(K, Present) ), Es2),
    append(Es1, Es2, Es).

price_effect(K, E) :-
    findall(V, ( node('Item', I), kind_of(I, K), prop(I, value, V), number(V), V > 0 ), Vs),
    length(Vs, Supply), sum_list(Vs, Sum), Value is Sum / Supply,
    ( good(K, base, B) -> Base = B ; Base = Supply ),
    ( good(K, demand, D0) -> true ; D0 = 0 ),
    const(shortage_demand, SD), aggregate_all(count, shortage(_, K), Short), D is D0 + SD * Short,
    factor(Base, Supply, D, F), Price is max(1, round(Value * F)),
    const(demand_decay, Decay), D1 is round(D * Decay * 1000) / 1000, F2 is round(F * 100) / 100,
    ( good(K, price, Prev) -> true ; Prev = null ),
    (   E = _{op: merge_node, label: 'MarketGood', name: K, props: _{base: Supply}}
    ;   member(Key-Val, [prev-Prev, price-Price, supply-Supply, factor-F2, demand-D1]),
        E = _{op: set_prop, entity: K, label: 'MarketGood', prop: Key, value: Val}
    ;   node('Item', I), kind_of(I, K), prop(I, value, V), number(V), V > 0,
        IP is max(1, round(V * F)), \+ prop(I, price, IP),
        E = _{op: set_prop, entity: I, label: 'Item', prop: price, value: IP}
    ).

% 有人买 → 这类东西更抢手；商人做完任何事，手上要卖的马上上架
after(buy(_, I, _, _), E) :-
    kind_of(I, K),
    ( E = _{op: merge_node, label: 'MarketGood', name: K, props: _{}}
    ; E = _{op: add_prop, entity: K, label: 'MarketGood', prop: demand, delta: 1} ).
after(Event, E) :-
    compound(Event), arg(1, Event, Who), atom(Who), unlisted(Who, I),
    (   listed(I, E)
    ;   format(atom(T), "Put ~w up for sale", [I]), E = _{op: note, text: T}
    ).

% ---------------------------------------------------------------------
% 商人想做的事、提示词、导演面板
% ---------------------------------------------------------------------

want(P, _{goal: 'Collect item', target: T, reason: R, priority: Pr, source: shop}) :-
    shortage(P, K), const(shop_priority, Pr),
    format(atom(T), "kind:~w", [K]), format(atom(R), "The shop has run out of ~w; get or make one to sell", [K]).

shortages(P, Ks) :- findall(K, shortage(P, K), Ks), Ks \== [].

context(P, T) :-
    shortages(P, Ks), atomic_list_concat(Ks, ', ', J),
    format(atom(T), "Your shop is out of: ~w. Gather, make or buy them (Collect item kind:<kind>) - what you hold goes on sale by itself.", [J]).
context(P, _, T) :- sells(P, Ks), atomic_list_concat(Ks, ', ', J), format(atom(T), "You run a shop selling: ~w.", [J]).
context(P, _, T) :-
    shortages(P, Ks), atomic_list_concat(Ks, ', ', J),
    format(atom(T), "Your shop is out of: ~w - you'd welcome anyone bringing some.", [J]).

panel(_, market, Rows) :-
    findall(K-_{kind: K, price: Pr, factor: F, supply: S, demand: D, trend: Tr},
            ( node('MarketGood', K), good(K, price, Pr), Pr \== null,
              ( good(K, factor, F) -> true ; F = 1.0 ), ( good(K, supply, S) -> true ; S = 0 ),
              ( good(K, demand, D0) -> D is round(D0 * 10) / 10 ; D = 0 ),
              ( good(K, prev, Prev), number(Prev), Prev =\= Pr -> ( Pr > Prev -> Tr = up ; Tr = down ) ; Tr = '' ) ),
            Ks),
    keysort(Ks, S0), pairs_values(S0, Rows).
