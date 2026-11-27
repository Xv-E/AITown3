:- encoding(utf8).
% =====================================================================
% base 包：打人之后。伤害、死亡由动作里的 strike op 结算（引擎）；这里管之后谁怎么看、算不算犯罪。
%   strike 会在打人的人身上记下 strike_provoked = 动手前的敌对标签（'' = 没被惹）。
% =====================================================================

const(player_assault_trust, -3).
const(hurt_victim, -4).     % 挨打的人对打人的人
const(hurt_ally, -3).       % 挨打的人的盟友
const(hurt_witness, -2).    % 看见的人（挨打的本来就是他的敌人则不算）

unprovoked(A) :- \+ ( prop(A, strike_provoked, Why), Why \== '' ).

after(attack(A, T, _), E) :- now(Now), struck(A, T, Now, E).

struck(A, T, Now, _{op: feel, a: T, b: A, delta: D, reason: 'attacked me', at: Now}) :- const(hurt_victim, D).
% 玩家动手：挨打的人对玩家的信任再降一截（NPC 之间没有个人信任这回事）
struck(A, T, Now, _{op: feel, a: T, b: A, delta: D, reason: 'the player attacked me', at: Now}) :-
    player(A), \+ player(T), const(player_assault_trust, D).
struck(A, T, Now, _{op: feel, a: X, b: A, delta: D, reason: Why, at: Now}) :-
    ally(T, X), X \== A, const(hurt_ally, D), format(atom(Why), "attacked ~w", [T]).
struck(A, T, Now, _{op: feel, a: W, b: A, delta: D, reason: Why, at: Now}) :-
    onlooker(A, T, W), \+ hostility(W, T, _),
    const(hurt_witness, D), format(atom(Why), "saw them attack ~w", [T]).
% 没被惹就动手：看见的人（包括挨打的）都记下来
struck(A, T, Now, _{op: link, a: W, a_label: 'Person', b: A, b_label: 'Person', rel: 'WITNESSED',
                    props: _{crime: assault, victim: T, item: '', at: Now}}) :-
    unprovoked(A), ( onlooker(A, T, W) ; W = T ).
struck(A, _, _, _{op: add_prop, entity: A, label: 'Person', prop: crimes, delta: 1}) :- unprovoked(A).
struck(A, T, _, _{op: report, crime: Crime, witnesses: Ws}) :-
    ( unprovoked(A) -> Crime = true ; Crime = false ),
    findall(W, onlooker(A, T, W), Ws).
struck(_, T, _, _{op: note, text: Text}) :-
    (   \+ alive(T) -> format(atom(Text), "~w is dead", [T])
    ;   prop(T, hp, HP), format(atom(Text), "~w has ~w HP left", [T, HP])
    ).
struck(A, _, _, _{op: note, text: "an unprovoked attack: witnesses will remember"}) :- unprovoked(A).

onlooker(A, T, W) :- sees(W, A), W \== T.
