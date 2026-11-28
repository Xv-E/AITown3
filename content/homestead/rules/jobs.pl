:- encoding(utf8).
% =====================================================================
% homestead 包：居民的职业和爱好。
%
% 据点就是一个群体：kind = homestead（特性 household）。它控制的地点（及其中的室内房间）就是「家」。
% 居民 = 它的直接成员，身上写 work（职业）和 hobby（爱好），技能写在 skills（JSON：{"farming": 3}）。
%
% 职业 / 爱好是数据（下面的 job/2、hobby/2），每样写：
%   skill     用哪门技能（每干一小时 +0.05）
%   facility  要家里有哪类设施（item_kind），人得站在设施所在的地点干
%   product   产出什么（原型），放到家里的储物处（item_kind = stockpile）旁边
%   uses      要用掉储物处的什么（原型），没有就干不了
%   rate      效率为 1 时每小时产出多少（攒够 1 件出 1 件）
% 要开始干活：自己有一张屋檐下的床；家里有对应设施；要产出东西的还得有储物处；不能饿坏、累垮。
% 效率 = (1 + 技能/10) × 床（0.7 + 0.1 × 品质）× 对当家人的好感（0.9 + 0.04 × 好感，夹在 0.5..1.3）
% 作息（没有写日程的居民）：8–17 点干活，17–21 点做爱好，夜里睡觉（needs.pl）。
% =====================================================================

:- discontiguous tick/2, want/2, context/2, panel/3, job/2, hobby/2.
:- multifile job/2, hobby/2.

kind_traits(homestead, [household, landed, homebound, enduring, close_knit, entry(sponsor, 2)]).

job(farmer,     _{skill: farming,     facility: field,          product: turnip,   rate: 0.4,  text: "tend the fields"}).
job(woodcutter, _{skill: woodcutting, facility: chopping_block, product: firewood, rate: 0.3,  text: "split firewood"}).
job(cook,       _{skill: cooking,     facility: kitchen_hearth, product: stew, uses: turnip, rate: 0.4, text: "cook for the household"}).
hobby(gardening, _{skill: gardening, facility: herb_bed,        product: herb,    rate: 0.12, text: "potter about the herb bed"}).
hobby(whittling, _{skill: crafting,  facility: whittling_bench, product: carving, rate: 0.08, text: "whittle little wooden figures"}).
hobby(music,     _{skill: music, cheer: true, text: "play music for whoever is around"}).

const(work_from, 8).
const(work_until, 17).
const(hobby_until, 21).
const(skill_per_hour, 0.05).

tick_phase(60, jobs).

reads(label('Item', [archetype, portable])).

% ---------------------------------------------------------------------
% 词汇
% ---------------------------------------------------------------------

household(P, G) :- direct(P, G), has(G, household), !.

task(P, work, J, D) :- prop(P, work, J), J \== '', job(J, D).
task(P, hobby, H, D) :- prop(P, hobby, H), H \== '', hobby(H, D).
task_state(work, working).
task_state(hobby, at_leisure).
progress_key(work, work_progress).
progress_key(hobby, hobby_progress).

skill(P, S, V) :-
    (   prop(P, skills, Raw), atom(Raw), catch(atom_json_dict(Raw, D, []), _, fail), get_dict(S, D, V0), number(V0)
    ->  V = V0 ; V = 0 ).

stockpile(P, S, L) :-
    household(P, _), node('Item', S), prop(S, item_kind, stockpile), rel('LOCATED_AT', S, L), home_place(P, L), !.
facility_at(P, Kind, F, L) :-
    node('Item', F), prop(F, item_kind, Kind), rel('LOCATED_AT', F, L), home_place(P, L).
input_at(Arch, L, I) :- node('Item', I), prop(I, archetype, Arch), rel('LOCATED_AT', I, L), \+ rel('HAS_ITEM', _, I).

leader_feeling(P, F) :- household(P, G), leader_of(G, L), L \== P, !, feeling(P, L, F).
leader_feeling(_, 0).

% 为什么还干不了（没有就是能干）
blocker(P, _, _, B) :- \+ household(P, _), B = "does not live on a homestead".
blocker(P, _, _, "has no bed of their own under a roof") :- \+ proper_bed(P).
blocker(P, _, D, B) :- get_dict(facility, D, Fac), \+ facility_at(P, Fac, _, _), format(atom(B), "no ~w on the homestead", [Fac]).
blocker(P, _, D, "no stockpile to put things in") :- get_dict(product, D, _), \+ stockpile(P, _, _).
blocker(P, _, D, B) :-
    get_dict(uses, D, U), ( stockpile(P, _, L) -> \+ input_at(U, L, _) ; true ),
    format(atom(B), "no ~w in the stockpile", [U]).
blocker(P, _, _, "too hungry or worn out") :-
    hunger(P, H), fatigue(P, F), const(starving_at, S), const(exhausted_at, X), ( H >= S ; F >= X ).

blockers(P, Kind, Bs) :- task(P, Kind, _, D), findall(B, blocker(P, Kind, D, B), Bs0), list_to_set(Bs0, Bs).
active(P, Kind) :- blockers(P, Kind, []).

efficiency(P, Kind, E) :-
    task(P, Kind, _, D), skill(P, D.skill, Sk),
    ( bed_of(P, B) -> bed_quality(B, Q) ; Q = 0 ),
    leader_feeling(P, F), A is max(0.5, min(1.3, 0.9 + 0.04 * F)),
    E0 is (1 + Sk / 10) * (0.7 + 0.1 * Q) * A, E is round(E0 * 100) / 100.

% 在哪儿干：有设施就去设施那儿；没有设施的（如奏乐）就在家里储物处那块地
workplace(P, Kind, L) :-
    task(P, Kind, _, D),
    (   get_dict(facility, D, Fac) -> once(facility_at(P, Fac, _, L))
    ;   stockpile(P, _, L) -> true
    ;   once(home_place(P, L))
    ).

work_hours :- hour(H), const(work_from, A), const(work_until, B), H >= A, H < B.
hobby_hours :- hour(H), const(work_until, A), const(hobby_until, B), H >= A, H < B.
hours(work) :- work_hours.
hours(hobby) :- hobby_hours.

scheduled(P) :- \+ ( prop(P, routine, R), R \== '', R \== '[]' ).

% ---------------------------------------------------------------------
% 想做的事：到点去干活 / 做爱好
% ---------------------------------------------------------------------

want(P, _{goal: 'Perform action', target: Act, at_location: L, reason: R, priority: Pr, source: Kind}) :-
    member(Kind-Act-Pr, [work-work-0.74, hobby-pastime-0.6]),
    has_needs(P), scheduled(P), hours(Kind), task_state(Kind, St), \+ in_state(P, St), \+ asleep(P),
    task(P, Kind, Id, D), active(P, Kind), workplace(P, Kind, L),
    format(atom(R), "~w: ~w (~w) at ~w", [Kind, Id, D.text, L]).

% 没别的事：在家里（储物处那块地）歇着，不到处乱逛、不把存货揣走
want(P, _{goal: 'Perform action', target: wait, at_location: L, reason: "Nothing to do right now: stay around home",
          priority: 0.2, source: home}) :-
    has_needs(P), scheduled(P), household(P, _), \+ asleep(P), \+ in_state(P, working), \+ in_state(P, at_leisure),
    ( stockpile(P, _, L) -> true ; once(home_place(P, L)) ).

% ---------------------------------------------------------------------
% 每小时：收工、产出、长技能
% ---------------------------------------------------------------------

tick(jobs, _{op: set_state, entity: P, state: null}) :-        % 到点收工 / 爱好时间过了
    every_hour, has_needs(P), scheduled(P),
    member(Kind-St, [work-working, hobby-at_leisure]), in_state(P, St), \+ hours(Kind).
tick(jobs, E) :-
    every_hour, has_needs(P), member(Kind, [work, hobby]),
    task_state(Kind, St), in_state(P, St), task(P, Kind, _, D), active(P, Kind),
    workplace(P, Kind, L), location(P, L),
    produce(P, Kind, D, L, E).

produce(P, _, D, _, E) :-                                        % 技能
    const(skill_per_hour, G), skill_json(P, D.skill, G, J),
    E = _{op: set_prop, entity: P, label: 'Person', prop: skills, value: J}.
produce(P, Kind, D, _, E) :-                                     % 攒进度，够一件出一件
    get_dict(product, D, Arch), get_dict(rate, D, Rate),
    efficiency(P, Kind, Eff), progress_key(Kind, K),
    ( prop(P, K, Old), number(Old) -> true ; Old = 0 ),
    New is Old + Rate * Eff, N0 is floor(New),
    stockpile(P, S, SL),
    (   get_dict(uses, D, U) -> findall(I, input_at(U, SL, I), Is0), sort(Is0, Is), length(Is, Have), N is min(N0, Have)
    ;   Is = [], N = N0
    ),
    Rest is round((New - N) * 1000) / 1000,
    (   E = _{op: set_prop, entity: P, label: 'Person', prop: K, value: Rest}
    ;   N > 0, between(1, N, _), E = _{op: spawn_item, archetype: Arch, located_at: SL, near: S}
    ;   N > 0, length(Used, N), append(Used, _, Is), member(I, Used), E = _{op: delete_entity, entity: I, label: 'Item'}
    ).
produce(P, _, D, L, _{op: set_prop, entity: O, label: 'Person', prop: mood, value: cheerful}) :-   % 奏乐：在场的人心情好
    get_dict(cheer, D, true), rel('LOCATED_AT', O, L), O \== P, node('Person', O), living(O).

skill_json(P, S, Gain, J) :-
    (   prop(P, skills, Raw), atom(Raw), catch(atom_json_dict(Raw, D0, []), _, fail), is_dict(D0) -> true ; D0 = _{} ),
    ( get_dict(S, D0, V0), number(V0) -> true ; V0 = 0 ),
    V is round((V0 + Gain) * 100) / 100, put_dict(S, D0, V, D1),
    with_output_to(string(J), json_write_dict(current_output, D1, [width(0)])).

% ---------------------------------------------------------------------
% 提示词、据点面板
% ---------------------------------------------------------------------

context(P, T) :-
    has_needs(P), member(Kind, [work, hobby]), task(P, Kind, Id, D),
    (   active(P, Kind)
    ->  efficiency(P, Kind, E), workplace(P, Kind, L),
        format(atom(T), "Your ~w: ~w — you ~w at ~w (efficiency ~2f).", [Kind, Id, D.text, L, E])
    ;   blockers(P, Kind, [B|_]),
        format(atom(T), "You cannot get on with your ~w (~w) yet: ~w.", [Kind, Id, B])
    ).

panel(_, home, Card) :-
    group(G), has(G, household),
    findall(R, (direct(P, G), node('Person', P), \+ player(P), resident_card(P, R)), Rs),
    findall(K-Is, ( stockpile_of(G, S, L), setof(I, (rel('LOCATED_AT', I, L), node('Item', I), I \== S,
                                                     prop(I, portable, true)), Is0),
                    member(I0, Is0), prop(I0, item_kind, K), include([X]>>prop(X, item_kind, K), Is0, Is) ), KIs0),
    sort(KIs0, KIs), findall(_{kind: K, count: N}, (member(K-Is, KIs), length(Is, N)), Stock),
    Card = _{homestead: G, residents: Rs, stockpile: Stock}.

stockpile_of(G, S, L) :- node('Item', S), prop(S, item_kind, stockpile), rel('LOCATED_AT', S, L), controller(L, G), !.

resident_card(P, _{name: P, work: W, hobby: H, working: Wa, work_blockers: WB, hobby_blockers: HB,
                   efficiency: E, hunger: Hu, fatigue: F, bed: Bed, state: St}) :-
    ( prop(P, work, W) -> true ; W = '' ), ( prop(P, hobby, H) -> true ; H = '' ),
    ( blockers(P, work, WB) -> true ; WB = [] ), ( blockers(P, hobby, HB) -> true ; HB = [] ),
    ( WB == [], task(P, work, _, _) -> Wa = true ; Wa = false ),
    ( efficiency(P, work, E) -> true ; E = 0 ),
    hunger(P, Hu), fatigue(P, F), ( bed_of(P, Bed) -> true ; Bed = '' ),
    ( prop(P, state, St) -> true ; St = '' ).
