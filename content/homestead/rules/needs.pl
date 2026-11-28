:- encoding(utf8).
% =====================================================================
% homestead 包：需求（饿、困）和床。
%
% 只管记着需求的人：人物身上有 hunger 属性（世界文件 props 里写，如 "hunger": 20）。没有的人照旧。
%   hunger   0 = 饱，100 = 饿坏了；醒着每小时 +3，睡着 +1；吃东西减掉（食物的 value × 3，见 base 的 use_item）
%   fatigue  0 = 精神，100 = 撑不住了；醒着每小时 +5；睡着按床恢复
% 床：item_kind = bed 的物品，owner = 谁的床，quality 1..5。
%   在自己的床上睡：每小时恢复 6 + 3 × 品质（床在室内再 +2）；没有床或睡在别处：每小时 4
%   没有床的人会自己认领一张没主的床（在自己群体的地盘上）
% NPC 想做的事（want）：饿了去吃（先吃手边的，再去吃家里存的），困了或到了夜里回自己的床睡
% 急事（react）：饿坏了手上又有吃的就马上吃；撑不住就地睡
% 时间（每个游戏小时，tick 阶段 needs）：需求涨落；睡够了天也亮了就醒；饿了、太累了就放下手上的活
% =====================================================================

:- discontiguous tick/2, want/2, react/2, context/2.

reads(label('Item', [quality, owner, item_kind, value])).
reads(label('Location', [indoor, parent])).

const(hunger_awake, 3).
const(hunger_asleep, 1).
const(fatigue_awake, 5).
const(rest_on_ground, 4).
const(hungry_at, 50).
const(starving_at, 85).
const(tired_at, 70).
const(exhausted_at, 95).
const(night_from, 22).
const(night_until, 6).

hour_period(P) :- day_length(L), P is L / 24.
tick_period(P) :- hour_period(P).
tick_phase(50, needs).

% ---------------------------------------------------------------------
% 词汇
% ---------------------------------------------------------------------

has_needs(P) :- node('Person', P), prop(P, hunger, _), living(P).
hunger(P, H) :- ( prop(P, hunger, H0), number(H0) -> H = H0 ; H = 0 ).
fatigue(P, F) :- ( prop(P, fatigue, F0), number(F0) -> F = F0 ; F = 0 ).
asleep(P) :- prop(P, state, asleep).
in_state(P, S) :- prop(P, state, S).

night :- hour(H), const(night_from, A), const(night_until, B), ( H >= A ; H < B ).

bed(B) :- node('Item', B), prop(B, item_kind, bed).
bed_of(P, B) :- bed(B), prop(B, owner, P), !.
bed_quality(B, Q) :- ( prop(B, quality, Q0), number(Q0) -> Q = Q0 ; Q = 1 ).
bed_place(P, L) :- bed_of(P, B), rel('LOCATED_AT', B, L).
indoor(L) :- prop(L, indoor, true).
% 自己有一张屋檐下的床
proper_bed(P) :- bed_place(P, L), indoor(L).

% 这一小时睡觉恢复多少
rest(P, R) :-
    (   bed_of(P, B), rel('LOCATED_AT', B, L), location(P, L)
    ->  bed_quality(B, Q), ( indoor(L) -> Roof = 2 ; Roof = 0 ), R is 6 + 3 * Q + Roof
    ;   const(rest_on_ground, R)
    ).

food(I) :- node('Item', I), prop(I, item_kind, K), memberchk(K, [food, meal, cooked]).
holds_food(P, I) :- rel('HAS_ITEM', P, I), food(I), !.

% 能去吃的东西：手上的，然后是同一处的，然后是自己群体地盘上的（家里的存货），值钱的（顶饱的）先吃
food_for(P, I) :- holds_food(P, I), !.
food_for(P, I) :-
    findall(NV-X, ( food(X), \+ rel('HAS_ITEM', _, X), \+ ( owner(X, O), O \== P ),
                    rel('LOCATED_AT', X, L), ( location(P, L) ; home_place(P, L) ),
                    prop(X, value, V), NV is -V ), Ks),
    keysort(Ks, [_-I|_]).

% P 的家：P 直接所属的群体控制的地点（及其中的室内房间）
home_place(P, L) :- direct(P, G), controller(L, G).
home_place(P, L) :- direct(P, G), prop(L, parent, Out), Out \== '', controller(Out, G).

% ---------------------------------------------------------------------
% 每小时
% ---------------------------------------------------------------------

tick(needs, E) :- every_hour, has_needs(P), need_effect(P, E).
tick(needs, E) :- every_hour, claim_beds(Pairs), member(P-B, Pairs),
    E = _{op: set_prop, entity: B, label: 'Item', prop: owner, value: P}.

every_hour :- hour_period(P), every(P).

need_effect(P, _{op: add_prop, entity: P, label: 'Person', prop: hunger, delta: D, max: 100}) :-
    ( asleep(P) -> const(hunger_asleep, D) ; const(hunger_awake, D) ).
need_effect(P, E) :-
    (   asleep(P) -> rest(P, R), D is -R, E = _{op: add_prop, entity: P, label: 'Person', prop: fatigue, delta: D, min: 0}
    ;   const(fatigue_awake, D), E = _{op: add_prop, entity: P, label: 'Person', prop: fatigue, delta: D, max: 100}
    ).
need_effect(P, E) :-                      % 饿坏了：掉血、心情很差（不会饿死，留一口气）
    hunger(P, H), H >= 100,
    ( E = _{op: add_prop, entity: P, label: 'Person', prop: hp, delta: -2, min: 1}
    ; E = _{op: set_prop, entity: P, label: 'Person', prop: mood, value: miserable} ).
need_effect(P, _{op: set_state, entity: P, state: null}) :-      % 睡够了、天也亮了 / 饿醒了
    asleep(P), fatigue(P, F), rest(P, R), hunger(P, H), const(starving_at, S),
    ( F - R =< 10, \+ night ; H >= S ).
need_effect(P, _{op: set_state, entity: P, state: null}) :-      % 饿了（家里有吃的）、累了：放下手上的活
    ( in_state(P, working) ; in_state(P, at_leisure) ),
    hunger(P, H), fatigue(P, F), ( H >= 60, food_for(P, _) ; F >= 85 ).

% 没床的人认领没主的床：都排好序一一配对（同一张床只给一个人）
claim_beds(Pairs) :-
    findall(P, (has_needs(P), \+ bed_of(P, _)), Ps0), sort(Ps0, Ps),
    findall(B, (bed(B), \+ ( prop(B, owner, O), O \== '' )), Bs0), sort(Bs0, Bs),
    pair_up(Ps, Bs, Pairs).

pair_up([], _, []) :- !.
pair_up(_, [], []) :- !.
pair_up([P|Ps], Bs, Pairs) :-
    (   select(B, Bs, Rest), rel('LOCATED_AT', B, L), home_place(P, L)
    ->  Pairs = [P-B|More], pair_up(Ps, Rest, More)
    ;   pair_up(Ps, Bs, Pairs)
    ).

% ---------------------------------------------------------------------
% 想做的事、急事
% ---------------------------------------------------------------------

want(P, _{goal: 'Use item', target: I, reason: R, priority: 0.86, source: needs}) :-
    has_needs(P), \+ asleep(P), hunger(P, H), const(hungry_at, Min), H >= Min, food_for(P, I),
    format(atom(R), "Hungry (~0f/100): eat ~w", [H, I]).
want(P, _{goal: 'Perform action', target: sleep, at_location: L, reason: R, priority: 0.8, source: needs}) :-
    has_needs(P), \+ asleep(P), fatigue(P, F), const(tired_at, T),
    ( F >= T ; night, F >= 30 ),
    ( bed_place(P, L) -> true ; location(P, L) ),
    format(atom(R), "Tired (~0f/100): go to bed at ~w", [F, L]).

react(P, _{goal: 'Eat', target: I, why: starving, text: T, source: needs, plan: [[consume, [P, I]]]}) :-
    has_needs(P), \+ player(P), \+ busy_since(P, _), \+ asleep(P),
    hunger(P, H), const(starving_at, S), H >= S, holds_food(P, I),
    format(atom(T), "~w is starving and wolfs down ~w", [P, I]).
react(P, _{goal: 'Sleep', target: P, why: exhausted, text: T, source: needs, plan: [[sleep, [P]]]}) :-
    has_needs(P), \+ player(P), \+ busy_since(P, _), \+ asleep(P),
    fatigue(P, F), const(exhausted_at, X), F >= X,
    format(atom(T), "~w can't keep their eyes open and lies down where they stand", [P]).

% ---------------------------------------------------------------------
% 提示词
% ---------------------------------------------------------------------

context(P, T) :- has_needs(P), hunger(P, H), hunger_word(H, W), format(atom(T), "You are ~w (hunger ~0f/100).", [W, H]).
context(P, T) :- has_needs(P), fatigue(P, F), fatigue_word(F, W), format(atom(T), "You feel ~w (fatigue ~0f/100).", [W, F]).
context(P, T) :-
    has_needs(P),
    (   bed_of(P, B), rel('LOCATED_AT', B, L), bed_quality(B, Q)
    ->  ( indoor(L) -> Roof = "under a roof" ; Roof = "out in the open" ),
        format(atom(T), "Your bed is ~w at ~w (quality ~w/5, ~w).", [B, L, Q, Roof])
    ;   T = "You have no bed of your own, so you sleep badly."
    ).

hunger_word(H, starving) :- const(starving_at, S), H >= S, !.
hunger_word(H, hungry) :- const(hungry_at, S), H >= S, !.
hunger_word(H, peckish) :- H >= 25, !.
hunger_word(_, 'well fed').
fatigue_word(F, exhausted) :- const(exhausted_at, S), F >= S, !.
fatigue_word(F, tired) :- const(tired_at, S), F >= S, !.
fatigue_word(F, 'a little worn') :- F >= 40, !.
fatigue_word(_, rested).
