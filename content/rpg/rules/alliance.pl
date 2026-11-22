:- encoding(utf8).
% rpg 包：提出结盟（actions/propose_alliance.json 的条件用 base 的 would_ally/2，
% 动作本身建好 ALLY 边）。之后：对方好感 +1，两人按 base 的 join_effect/4 聚成一伙。
after(propose_alliance(C, O, _), E) :-
    now(T),
    (   E = _{op: feel, a: O, b: C, delta: 1, reason: 'became allies', at: T}
    ;   join_effect(C, O, T, E)
    ).
