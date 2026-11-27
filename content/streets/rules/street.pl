:- encoding(utf8).
% =====================================================================
% streets 包：街头经济。钱只在街上流转，从不凭空出现或消失。
%
% 钱：item_kind = cash 的物品（一张一元钞票），每笔交易都是钞票换手（transfer 效果）。
%   买东西   顾客把钱付给摊档（cash 原型的 effects_on）→ 钱进摊主口袋
%   保护费   有 extorts 特性的群体控制的地盘上，每家生意（Item.owner + protection_fee）按天欠下保护费，
%            记在 (:Faction)-[:EXTORTS {dues, paid, fee}]->(:Item)；成员 collect_dues 时收走，老大抽一半
%   工钱     群体写了 wage：首领每天给每个成员发工钱
%   房租     rent_book：租客每天交房租给业主
% 和群体联动：
%   交保护费的地点，收钱的群体控制力上升；别的群体来「过界收数」会抢走对头的钱、引发张力；
%   有 polices 特性的群体的成员在场时收不了，还会被记一笔；
%   拖欠满 overdue_days 天就砸场：存货清零、摊主受伤，和摊主所在的 trade_guild 群体结怨。
% =====================================================================

:- discontiguous tick/2, after/2, context/2, context/3, panel/3, plea/1.

reads(label('Item', [owner, protection_fee, stock, takings, landlord, rent, tenants, arrears])).
reads(rel('EXTORTS')).
reads(rel('HAS_ITEM')).

const(overdue_days, 3.0).
const(boss_cut, 0.5).
const(turf_per_payment, 0.5).
const(turf_per_new_racket, 1.0).
const(muscle_in_standing, -3.0).
const(muscle_in_tension, 2.0).
const(police_standing, -1.0).
const(smash_hp, 10).

tick_period(L) :- day_length(L).
day_due :- day_length(L), every(L).

tick_phase(200, dues).
tick_phase(210, overdue).
tick_phase(220, wages).
tick_phase(230, rent).

% ---------------------------------------------------------------------
% 词汇
% ---------------------------------------------------------------------

money_world :- once(( node('Item', I), prop(I, item_kind, cash) )).
cash(P, N) :- aggregate_all(count, (rel('HAS_ITEM', P, I), prop(I, item_kind, cash)), N).

business(B, Owner, Loc, Fee) :-
    node('Item', B), prop(B, protection_fee, Fee), number(Fee), Fee > 0,
    prop(B, owner, Owner), Owner \== '', rel('LOCATED_AT', B, Loc).
racket(G, B, Dues) :- rel('EXTORTS', G, B, Ps), kv(Ps, dues, 0.0, Dues).

collects_for(P, G) :- memberships(P, Ms), member(m(G, _, ''), Ms), has(G, extorts).
gang_mates(G, P) :- rel('MEMBER_OF', P, G).

dues_effect(G, B, Dues, Paid, Fee, Es) :-
    D is round(max(0.0, Dues) * 100) / 100,
    Es = [ _{op: set_rel_prop, a: G, a_label: 'Faction', b: B, b_label: 'Item', rel: 'EXTORTS', prop: dues, value: D},
           _{op: set_rel_prop, a: G, a_label: 'Faction', b: B, b_label: 'Item', rel: 'EXTORTS', prop: fee, value: Fee},
           _{op: add_rel_prop, a: G, a_label: 'Faction', b: B, b_label: 'Item', rel: 'EXTORTS', prop: paid, delta: Paid} ].

pay(From, To, N, _{op: transfer, from: From, to: To, kind: cash, count: N}).

turf_effect(G, L, Amount, _{op: add_rel_prop, a: G, a_label: 'Faction', b: L, b_label: 'Location', rel: 'CONTROLS',
                           prop: strength, delta: Amount, max: Max}) :- const(strength_max, Max).

tension_effects(A, B, Amount, Es) :-
    (   group_rel(A, B, Ps) -> kv(Ps, tension, 0.0, T0), T is T0 + Amount, rel_prop_effect(A, B, tension, T, E), Es = [E]
    ;   stance_effects(A, B, neutral, "money on the same streets", Amount, Es)
    ).

% ---------------------------------------------------------------------
% 保护费累计：控制这块地的群体会收数，就欠它的；不再控制的失去收数的权利
% ---------------------------------------------------------------------

tick(dues, E) :-
    group_due, business(B, _, L, Fee),
    ( controller(L, C), has(C, extorts) -> Holder = C ; Holder = '' ),
    (   racket(G, B, _), G \== Holder,
        E = _{op: unlink, a: G, a_label: 'Faction', b: B, b_label: 'Item', rel: 'EXTORTS'}
    ;   Holder \== '', ( racket(Holder, B, D0) -> true ; D0 = 0.0 ),
        const(group_tick_hours, H), D is D0 + Fee * H / 24,
        dues_effect(Holder, B, D, 0, Fee, Es), member(E, Es)
    ).

% ---------------------------------------------------------------------
% 每天：拖欠的砸场、工钱、房租
% ---------------------------------------------------------------------

tick(overdue, E) :-
    day_due, business(B, Owner, L, Fee), racket(G, B, Dues), const(overdue_days, Days), Dues >= Fee * Days,
    const(smash_hp, Hp), NHp is -Hp,
    findall(Gu, (direct(Owner, Gu), has(Gu, trade_guild)), Guilds),
    format(atom(T), "~w smashed up ~w's ~w at ~w: ~0f dollars behind on protection.", [G, Owner, B, L, Dues]),
    format(atom(M), "~w smashed your ~w because you were behind on protection money.", [G, B]),
    (   E = _{op: set_prop, entity: B, label: 'Item', prop: stock, value: 0}
    ;   E = _{op: set_prop, entity: B, label: 'Item', prop: smashed_by, value: G}
    ;   E = _{op: add_prop, entity: Owner, label: 'Person', prop: hp, delta: NHp, min: 1}
    ;   E = _{op: set_prop, entity: Owner, label: 'Person', prop: mood, value: afraid}
    ;   dues_effect(G, B, Fee, 0, Fee, Es), member(E, Es)
    ;   member(Gu, Guilds), tension_effects(G, Gu, 1.0, Es), member(E, Es)
    ;   append([G], Guilds, Gs), chronicle(economy, T, Gs, L, E)
    ;   E = _{op: remember, who: Owner, text: M, type: economy, location: L}
    ).

tick(wages, E) :-
    day_due, group(G), prop(G, wage, W0), number(W0), W0 > 0, W is truncate(W0), leader_of(G, Boss),
    findall(P, (gang_mates(G, P), P \== Boss), Ps0), sort(Ps0, Ps), cash(Boss, Cash),
    wage_plan(Ps, W, Cash, Plan), member(P-Paid, Plan),
    (   Paid > 0
    ->  format(atom(N), "~w paid ~w $~w (~w)", [Boss, P, Paid, G]),
        format(atom(M), "~w paid you $~w for your work with ~w.", [Boss, Paid, G]),
        ( pay(Boss, P, Paid, E) ; E = _{op: note, text: N} ; E = _{op: remember, who: P, text: M, type: economy} )
    ;   format(atom(N), "~w couldn't pay ~w (~w)", [Boss, P, G]),
        format(atom(M), "~w had no money to pay you today.", [Boss]),
        ( E = _{op: note, text: N} ; E = _{op: remember, who: P, text: M, type: economy} )
    ).

% 首领的钱发到哪个人就没了
wage_plan([], _, _, []).
wage_plan([P|Ps], W, Cash, [P-Paid|Rest]) :- Paid is min(W, Cash), Left is Cash - Paid, wage_plan(Ps, W, Left, Rest).

tick(rent, E) :-
    day_due, node('Item', Book), prop(Book, item_kind, rent_book), prop(Book, landlord, Lord), Lord \== '',
    ( prop(Book, rent, R0), number(R0) -> Rent is truncate(R0) ; Rent = 1 ),
    ( prop(Book, tenants, Ts0) -> tenant_list(Ts0, Ts) ; Ts = [] ),
    ( prop(Book, arrears, A0), atom(A0), catch(atom_json_dict(A0, Arr0, []), _, fail) -> true ; Arr0 = _{} ),
    ( rel('LOCATED_AT', Book, Loc) -> true ; Loc = '' ),
    findall(T-Paid, (member(T, Ts), T \== Lord, cash(T, C), Paid is min(Rent, C)), Plan),
    foldl(arrears_step, Plan, Arr0, Arr),
    with_output_to(string(AJ), json_write_dict(current_output, Arr, [width(0)])),
    (   E = _{op: set_prop, entity: Book, label: 'Item', prop: arrears, value: AJ}
    ;   member(T-Paid, Plan),
        (   Paid > 0
        ->  format(atom(N), "~w paid ~w $~w rent", [T, Lord, Paid]), ( pay(T, Lord, Paid, E) ; E = _{op: note, text: N} )
        ;   get_dict(T, Arr, Behind),
            format(atom(N), "~w is ~w day(s) behind on rent to ~w", [T, Behind, Lord]),
            format(atom(M), "You couldn't pay your rent to ~w today (~w days behind).", [Lord, Behind]),
            ( E = _{op: note, text: N} ; E = _{op: remember, who: T, text: M, type: economy, location: Loc} )
        )
    ).

tenant_list(L, L) :- is_list(L), !.
tenant_list(A, L) :- atom(A), catch(atom_json_term(A, L0, []), _, fail), is_list(L0), !, maplist([S, X]>>atom_string(X, S), L0, L).
tenant_list(_, []).

arrears_step(T-Paid, A0, A) :-
    atom_string(T, K0), atom_string(K, K0),
    (   Paid > 0 -> ( del_dict(K, A0, _, A) -> true ; A = A0 )
    ;   ( get_dict(K, A0, N0) -> true ; N0 = 0 ), N is N0 + 1, put_dict(K, A0, N, A)
    ).

% ---------------------------------------------------------------------
% 收数（collect_dues 动作之后）
% ---------------------------------------------------------------------

after(collect_dues(C), E) :- collect(C, Es), member(E, Es).

collect(C, Es) :-
    \+ location(C, _), !, Es = [_{op: note, text: "You are nowhere"}, _{op: report, collected: 0}].
collect(C, Es) :-
    \+ collects_for(C, _), !, format(atom(T), "~w has no gang to collect for", [C]),
    Es = [_{op: note, text: T}, _{op: report, collected: 0}].
collect(C, Es) :-
    location(C, L), once(collects_for(C, G)),
    findall(Cop-PG, (direct(Cop, PG), has(PG, polices), Cop \== C, living(Cop), location(Cop, L)), [Cop-PG|_]), !,
    const(police_standing, D), standing(C, PG, V0), V1 is max(-10.0, min(10.0, V0 + D)), signed(V1, S1),
    format(atom(T), "~w is watching — no money changes hands here today (~w standing ~w)", [Cop, PG, S1]),
    format(atom(M), "You caught ~w of ~w trying to collect protection at ~w.", [C, G, L]),
    Es = [ _{op: add_rel_prop, a: C, a_label: 'Person', b: PG, b_label: 'Faction', rel: 'STANDING',
             prop: value, delta: D, min: -10.0, max: 10.0},
           _{op: note, text: T},
           _{op: remember, who: Cop, text: M, type: economy, location: L},
           _{op: report, collected: 0} ].
collect(C, Es) :-
    location(C, L), once(collects_for(C, G)), findall(X, collects_for(C, X), Mine),
    findall(B, business(B, _, L, _), Bs0), sort(Bs0, Bs),
    foldl(collect_one(C, G, Mine, L), Bs, s([], [], 0, []), s(Es0, Notes, Total, Turf)),
    sum_by_key(Turf, TurfSums),
    findall(E, (member(TG-Amt, TurfSums), turf_effect(TG, L, Amt, E)), TurfEs),
    ( Notes == [] -> AllNotes = ["No business here to collect from"] ; AllNotes = Notes ),
    findall(_{op: note, text: N}, member(N, AllNotes), NoteEs),
    append([Es0, TurfEs, NoteEs, [_{op: report, collected: Total}]], Es).

% 一家一家收：s(效果, 告诉收数人的话, 收到多少, 地盘增量 [群体-数量])
collect_one(C, _, _, _, B, S, S) :- business(B, Owner, _, _), ( Owner == C ; once(( collects_for(C, G), gang_mates(G, Owner) )) ), !.
collect_one(C, G, Mine, L, B, s(Es0, Ns0, T0, Tu0), s(Es, Ns, T, Tu)) :-
    business(B, Owner, _, Fee),
    ( member(MG, Mine), racket(MG, B, MD) -> MineR = r(MG, MD) ; MineR = none ),
    ( racket(RG, B, RD), \+ memberchk(RG, Mine) -> TheirR = r(RG, RD) ; TheirR = none ),
    owner_cash(Owner, Es0, Cash),
    (   MineR == none, TheirR == none
    ->  format(atom(Chron), "~w started taking protection money from ~w's ~w at ~w.", [G, Owner, B, L]),
        findall(E, chronicle(economy, Chron, [G], L, E), ChronEs),
        dues_effect(G, B, Fee, 0, Fee, Start), const(turf_per_new_racket, NT),
        append([Es0, Start, ChronEs], Es1), Tu1 = [G-NT|Tu0],
        take(C, Owner, B, L, Fee, r(G, Fee), Cash, Es1, Ns0, T0, Tu1, Es, Ns, T, Tu)
    ;   MineR \== none
    ->  take(C, Owner, B, L, Fee, MineR, Cash, Es0, Ns0, T0, Tu0, Es, Ns, T, Tu)
    ;   TheirR = r(Rival, RD), Amount is min(truncate(RD), Cash),
        (   Amount =< 0
        ->  format(atom(N), "~w pays ~w, and has nothing left for you", [Owner, Rival]),
            append(Ns0, [N], Ns), Es = Es0, T = T0, Tu = Tu0
        ;   pay(Owner, C, Amount, PayE), dues_effect(Rival, B, RD - Amount, 0, Fee, DuesEs),
            const(muscle_in_standing, MS), standing(C, Rival, SV0), SV1 is max(-10.0, min(10.0, SV0 + MS)), signed(SV1, SS),
            const(muscle_in_tension, MT), tension_effects(G, Rival, MT, TenEs),
            format(atom(N), "muscled in on ~w: ~w paid $~w (~w standing ~w)", [Rival, Owner, Amount, Rival, SS]),
            format(atom(Chron), "~w of ~w took $~w from ~w at ~w — money ~w counts as theirs.", [C, G, Amount, Owner, L, Rival]),
            findall(E, chronicle(economy, Chron, [G, Rival], L, E), ChronEs),
            StE = _{op: add_rel_prop, a: C, a_label: 'Person', b: Rival, b_label: 'Faction', rel: 'STANDING',
                    prop: value, delta: MS, min: -10.0, max: 10.0},
            append([Es0, [PayE], DuesEs, [StE], TenEs, ChronEs], Es),
            append(Ns0, [N], Ns), T is T0 + Amount, const(turf_per_new_racket, NT), Tu = [G-NT|Tu0]
        )
    ).

% 收自己群体的数
take(C, Owner, B, L, Fee, r(MG, MD), Cash, Es0, Ns0, T0, Tu0, Es, Ns, T, Tu) :-
    Amount is min(truncate(MD), Cash),
    (   Amount =< 0
    ->  (   MD >= 1 -> format(atom(N), "~w can't pay right now (owes ~1f)", [Owner, MD])
        ;   format(atom(N), "~w owes nothing yet", [Owner])
        ),
        append(Ns0, [N], Ns), Es = Es0, T = T0, Tu = Tu0
    ;   pay(Owner, C, Amount, PayE), dues_effect(MG, B, MD - Amount, Amount, Fee, DuesEs),
        const(boss_cut, K), Cut0 is truncate(Amount * K),
        ( leader_of(MG, Boss), Boss \== C, Cut0 > 0 -> pay(C, Boss, Cut0, CutE), CutEs = [CutE], Cut = Cut0 ; CutEs = [], Cut = 0, Boss = '' ),
        (   Cut > 0 -> format(atom(N), "~w paid $~w ($~w goes up to ~w)", [Owner, Amount, Cut, Boss]),
            format(atom(MC), "You collected $~w from ~w at ~w for ~w; $~w went up to ~w.", [Amount, Owner, L, MG, Cut, Boss])
        ;   format(atom(N), "~w paid $~w", [Owner, Amount]),
            format(atom(MC), "You collected $~w from ~w at ~w for ~w.", [Amount, Owner, L, MG])
        ),
        format(atom(MO), "You paid ~w of ~w $~w protection money at ~w.", [C, MG, Amount, L]),
        append([Es0, [PayE], DuesEs, CutEs,
                [_{op: remember, who: Owner, text: MO, type: economy, location: L},
                 _{op: remember, who: C, text: MC, type: economy, location: L}]], Es),
        append(Ns0, [N], Ns), T is T0 + Amount, const(turf_per_payment, TP), Tu = [MG-TP|Tu0]
    ).

% 前面几家已经从同一个店主口袋里拿走的钱不能再拿
owner_cash(Owner, Es, Cash) :-
    cash(Owner, C0),
    aggregate_all(sum(N), (member(E, Es), is_dict(E), get_dict(op, E, transfer), get_dict(from, E, Owner), get_dict(count, E, N)), Out),
    Cash is max(0, C0 - Out).

% ---------------------------------------------------------------------
% 提示词、界面、剧情素材
% ---------------------------------------------------------------------

context(Me, T) :- money_line(Me, T).
context(Me, _, T) :- money_line(Me, T).

money_line(Me, T) :-
    money_world, \+ player(Me),
    findall(X, money_bit(Me, X), Xs), top(6, Xs, Top), member(T, Top).

money_bit(Me, T) :- cash(Me, N), format(atom(T), "Money: you have $~w. Buying food or drink puts your money in the owner's pocket.", [N]).
money_bit(Me, T) :-
    findall(G, collects_for(Me, G), Gs), Gs \== [],
    business(B, Owner, L, _), racket(G, B, D), memberchk(G, Gs), D >= 1,
    format(atom(T), "~w's ~w at ~w owes ~w $~0f (go there and collect_dues)", [Owner, B, L, G, D]).
money_bit(Me, T) :-
    business(B, Me, _, Fee), const(overdue_days, Days),
    (   racket(G, B, D), format(atom(T), "You owe ~w $~1f protection for ~w (fee $~0f/day; ~0f days behind and they smash the place).", [G, D, B, Fee, Days])
    ;   \+ racket(_, B, _), format(atom(T), "Nobody takes protection from your ~w right now.", [B])
    ;   ( prop(B, stock, S) -> true ; S = 0 ), format(atom(T), "~w has ~w left in stock.", [B, S])
    ).

% 界面：钱的分布、每家生意
panel(P, economy, _{cash: C, richest: Top, money_supply: Supply, businesses: Bs}) :-
    money_world, cash(P, C),
    findall(NN-_{name: Q, cash: N}, (node('Person', Q), cash(Q, N), N > 0, NN is -N), Ks), keysort(Ks, S), pairs_values(S, All),
    top(5, All, Top), aggregate_all(sum(N), member(_{name: _, cash: N}, All), Supply),
    findall(_{name: B, owner: O, location: L, stock: St, fee: F, takings: Tk, protected_by: Gs, dues: D},
            ( business(B, O, L, F), ( prop(B, stock, St) -> true ; St = 0 ), ( prop(B, takings, Tk) -> true ; Tk = 0 ),
              findall(G, racket(G, B, _), Gs), aggregate_all(sum(X), racket(_, B, X), D0), round1(D0, D) ), Bs0),
    sort(name, @<, Bs0, Bs).

% 剧情素材：谁有难处、需要什么（Storyteller 会安排他去找玩家求助）
plea(_{who: O, need: cash, severity: Sev, title: Title, why: Why, arc: Arc, stakes: Stakes, note: Note,
       people: [O, G], reward: "I'll owe you, and I don't forget"}) :-
    business(B, O, _, Fee), racket(G, B, D), D >= Fee, Sev is D / max(1.0, Fee),
    format(atom(Title), "~w owes ~w", [O, G]),
    format(atom(Why), "~w want the $~0f I owe for ~w, and I can't pay", [G, D, B]),
    format(atom(Arc), "~w are squeezing ~w; someone has to help or it ends badly.", [G, O]),
    format(atom(Stakes), "~w smash ~w if ~w can't pay", [G, B, O]),
    format(atom(Note), "~w is $~0f behind with ~w.", [O, D, G]).
