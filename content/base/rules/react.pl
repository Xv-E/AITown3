:- encoding(utf8).
% =====================================================================
% base 包：NPC 看到敌人时打、逃还是不理（引擎的 react/2 钩子）。
%   打得过就打（不在一个地点就先走过去），快不行了又打不过就逃到旁边敌人最少的地点；
%   只是讨厌（不是被打）而又不占上风：不招惹。
% 正在做别的事（busy_since）：只有在开始之后又挨了打才打断。
% =====================================================================

reads(rel('LOCATED_AT')).
reads(rel('ADJACENT_TO')).
reads(label('Item', [damage, armor])).

const(flee_at, 0.3).        % HP 掉到最大值的这么多以下、又打不过，就逃
const(brave_ratio, 1.3).    % 只是讨厌（不是被打）的话，强出这么多才动手

% 会主动动手的原因
aggressive(attacked_me(_)).
aggressive(fighting(_)).
aggressive(faction(_)).
aggressive(criminal(_, _, _)).

neighbour(A, B) :- rel('ADJACENT_TO', A, B).
hp(P, H) :- ( prop(P, hp, H0) -> H = H0 ; H = 0 ).
power(P, X) :- ( prop(P, power_level, X0) -> X = X0 ; X = 1 ).
weapon(P, W) :- aggregate_all(max(D), (rel('HAS_ITEM', P, I), prop(I, damage, D)), W0), !, W = W0.
weapon(_, 0).
armour(P, A) :- aggregate_all(sum(X), (rel('HAS_ITEM', P, I), prop(I, armor, X)), A).

% 大概打得过谁：每一击的伤害 × 能挨几下
strength(P, S) :-
    power(P, Pw), weapon(P, W), hp(P, H), armour(P, A),
    S is max(1, Pw + W) * max(1, H + A * 3).

% 看得见的敌人，最近打过我的排前面
enemies(Me, Enemies) :-
    findall((R-O)-(O-Why), (sees(O, Me), hostility(Me, O, Why), rank(Why, R)), Keyed),
    keysort(Keyed, Sorted), pairs_values(Sorted, Enemies).

rank(attacked_me(_), 0) :- !.
rank(fighting(_), 1) :- !.
rank(_, 2).

hit_since(Me, T0) :- prop(Me, attacked_at, A), A >= T0.

react(Me, Choice) :-
    alive(Me), \+ player(Me),
    ( busy_since(Me, T0) -> hit_since(Me, T0) ; true ),
    enemies(Me, Enemies), Enemies = [Target-Why|_],
    label(Why, Label),
    location(Me, Here),
    strength(Me, Mine),
    aggregate_all(sum(S), (member(E-W, Enemies), ( aggressive(W) ; hostility(E, Me, _) ), strength(E, S)), Threat),
    hp(Me, HP), max_hp(Me, Max0), Max is max(Max0, max(HP, 1)), const(flee_at, FleeAt),
    (   HP =< FleeAt * Max, Threat > Mine
    ->  flee(Me, Here, Target, Label, Choice)
    ;   fight(Me, Here, Target, Why, Label, Mine, Choice)
    ).

flee(Me, Here, From, Label, _{goal: 'Flee', target: Dest, why: Label, text: Text, source: combat,
                              plan: [[travel, [Me, Here, Dest]]]}) :-
    findall((N-P)-P, (neighbour(Here, P), hostiles_at(Me, P, N)), Options), Options \== [],
    keysort(Options, [_-Dest|_]),
    format(atom(Text), "Flees from ~w to ~w", [From, Dest]).

hostiles_at(Me, Place, N) :-
    aggregate_all(count, (rel('LOCATED_AT', P, Place), node('Person', P), P \== Me, hostility(P, Me, _)), N).

fight(Me, Here, Target, Why, Label, Mine, _{goal: 'Defeat enemy', target: Target, why: Label, text: Text,
                                           source: combat, plan: Plan}) :-
    (   aggressive(Why) -> true
    ;   strength(Target, Theirs), const(brave_ratio, B), Mine >= B * Theirs
    ),
    location(Target, There),
    (   There == Here -> Plan = [[attack, [Me, Target, There]]]
    ;   neighbour(Here, There) -> Plan = [[travel, [Me, Here, There]], [attack, [Me, Target, There]]]
    ),
    format(atom(Text), "Fights ~w (~w)", [Target, Label]).
