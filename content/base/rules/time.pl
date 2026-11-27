:- encoding(utf8).
% =====================================================================
% base 包：时间过去会怎样（引擎每拍问 tick/1；跨过 tick_period 才真的问）。
%   好感朝 0 回落（盟友不低于 ally_floor），矛盾（TENSION）消退，没在打架的人回血。
% 结盟 / 散伙 / 朋友结的伙在 group_life.pl（同一周期，分步）。
% =====================================================================

reads(rel('TENSION')).

const(decay_every, 25.0).   % 模拟秒（两个游戏小时）
const(ally_floor, 3).       % 盟友之间的好感不会回落到这以下
const(regen, 8).            % 每个周期回这么多 HP（最近没挨打的人）

tick_period(P) :- const(decay_every, P).

tick(_{op: add_rel_prop, a: A, a_label: 'Person', b: B, b_label: 'Person', rel: 'FEELS', prop: value, delta: D}) :-
    const(decay_every, P), every(P),
    rel_prop('FEELS', A, B, value, V),
    (   V > 0, ( \+ ally(A, B) ; const(ally_floor, F), V > F ) -> D = -1
    ;   V < 0 -> D = 1
    ).
tick(_{op: add_rel_prop, a: A, a_label: 'Person', b: B, b_label: 'Person', rel: 'TENSION', prop: level, delta: -1}) :-
    const(decay_every, P), every(P),
    rel_prop('TENSION', A, B, level, L), L > 0.
tick(_{op: add_prop, entity: Who, label: 'Person', prop: hp, delta: R, max: Max}) :-
    const(decay_every, P), every(P),
    alive(Who), prop(Who, hp, HP), max_hp(Who, Max), HP < Max,
    \+ recently_attacked(Who), const(regen, R).

max_hp(Who, Max) :- ( prop(Who, max_hp, M) -> Max = M ; Max = 100 ).
recently_attacked(Who) :- prop(Who, attacked_at, T), now(N), const(combat_memory, M), N - T =< M.
